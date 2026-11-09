import json
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import cint, escape_html, now_datetime

from chatsync import formatting, messages, networks, sync
from chatsync.beeper import BeeperError
from chatsync.matching import (
	counterpart,
	match_chat,
	name_index,
	name_suggestion,
	phone_contacts,
	phone_index,
)
from chatsync.merges import expand, is_merged
from chatsync.phone import normalize

UNASSIGNED_TIMEOUT = 5
PHONE_NETWORKS = {"whatsapp", "signal"}
PREVIEW_LIMIT = 100
MERGED_SEARCH_LIMIT = 10
INBOX_PAGE_SIZE = 50


@contextmanager
def assignment_writes():
	frappe.flags.in_chat_assignment = True
	try:
		yield
	finally:
		frappe.flags.in_chat_assignment = False


def session_account():
	name = frappe.db.get_value("Chat Account", {"user": frappe.session.user, "enabled": 1})
	if not name:
		frappe.throw(frappe._("No enabled Chat Account for {0}").format(frappe.session.user))
	return frappe.get_doc("Chat Account", name)


def beeper_call(function, *args):
	try:
		return function(*args)
	except BeeperError as e:
		frappe.throw(str(e))


def direct_chat(client, beeper_chat_id: str):
	raw = beeper_call(client.chat, beeper_chat_id)
	if is_merged(raw):
		frappe.throw(frappe._("This is a merged Beeper chat, pick one of its chats instead."))
	cp = counterpart(raw)
	if not cp:
		frappe.throw(frappe._("Only direct chats can be assigned"))
	return raw, cp


def summary(raw: dict, cp) -> dict:
	return {
		"beeper_chat_id": cp.beeper_chat_id,
		"network": cp.network,
		"title": raw.get("title"),
		"phone": cp.phone,
		"username": cp.username,
		"last_activity": raw.get("lastActivity"),
	}


def check_contact(contact: str) -> None:
	if not frappe.has_permission("Contact", "read", doc=contact):
		frappe.throw(frappe._("No permission for Contact {0}").format(contact), frappe.PermissionError)


def check_chat(chat: str, write: bool = False) -> None:
	"""Reading follows the permission rules; changing a chat is for its owner only."""
	allowed = frappe.has_permission("Chat", "read", doc=chat)
	if write:
		allowed = allowed and frappe.db.get_value("Chat", chat, "user") == frappe.session.user
	if not allowed:
		frappe.throw(frappe._("No permission for Chat {0}").format(chat), frappe.PermissionError)


def drop_rows(beeper_chat_id: str, contact: str | None = None) -> None:
	filters = {"parenttype": "Contact", "beeper_chat_id": beeper_chat_id}
	if contact:
		filters["parent"] = contact
	frappe.db.delete("Chat Identifier", filters)


def add_row(contact: str, values: dict) -> None:
	"""Written as a child row: saving the Contact would fire the host's hooks (pushes to other systems)."""
	rows = frappe.get_all(
		"Chat Identifier", filters={"parenttype": "Contact", "parent": contact}, pluck="idx"
	)
	frappe.get_doc(
		{
			"doctype": "Chat Identifier",
			"parenttype": "Contact",
			"parentfield": "chat_identifiers",
			"parent": contact,
			"idx": max(rows, default=0) + 1,
			**values,
		}
	).insert(ignore_permissions=True)


def searched_members(client, member_client, query: str, limit: int = 20):
	"""Beeper's search hits (any age) with merged chats replaced by their members."""
	cache: dict = {}
	merged_left = MERGED_SEARCH_LIMIT
	for found in client.search_chats(query, limit):
		if is_merged(found):
			if not merged_left:
				continue
			merged_left -= 1
		yield from expand(member_client, found, cache)


@frappe.whitelist()
def search_chats(query: str) -> list[dict]:
	account = session_account()
	client = sync.client_for(account)
	member_client = sync.client_for(account, timeout=UNASSIGNED_TIMEOUT)
	result = {}
	for raw in beeper_call(list, searched_members(client, member_client, query)):
		if cp := counterpart(raw):
			result.setdefault(cp.beeper_chat_id, summary(raw, cp))
	return list(result.values())


def unassigned_items(account, raws, include_ignored: bool = False) -> list[dict]:
	"""The direct chats among `raws` that no contact takes, with what could be assigned instead."""
	index, names = phone_index(), name_index()
	ignored = ignored_chats(account.user)
	result = {}
	for raw in raws:
		cp = counterpart(raw)
		if not cp or cp.beeper_chat_id in result:
			continue
		hidden = cp.beeper_chat_id in ignored
		if hidden and not include_ignored:
			continue
		found = match_chat(account, cp, index=index)
		if found.contact or found.excluded:
			continue
		existing = phone_contacts(account.user, cp.phone, index) if cp.phone else []
		suggested = None if cp.phone else name_suggestion(account.user, [raw.get("title"), cp.name], names)
		result[cp.beeper_chat_id] = {
			**summary(raw, cp),
			"network_label": networks.label(cp.network),
			"ambiguous": found.ambiguous,
			"existing_contact": existing[0] if len(existing) == 1 else None,
			"ignored": hidden,
			"suggested_contact": suggested,
			"suggested_contact_name": contact_name(suggested),
		}
	return list(result.values())


def find_unassigned_chats(
	user: str | None = None, days: int = 14, include_ignored: bool = False
) -> list[dict]:
	"""Direct chats active in the last `days` that no contact takes. Raises BeeperError."""
	name = frappe.db.get_value("Chat Account", {"user": user or frappe.session.user, "enabled": 1})
	if not name:
		return []
	account = frappe.get_doc("Chat Account", name)
	client = sync.client_for(account, timeout=UNASSIGNED_TIMEOUT)
	threshold = now_datetime() - timedelta(days=int(days))
	return unassigned_items(account, sync.walk_members(client, threshold), include_ignored)


@frappe.whitelist(methods=["GET"])
def unassigned_page(cursor: str | None = None, query: str | None = None, include_ignored: int = 0) -> dict:
	"""One Beeper page of the caller's unassigned direct chats, any age, live; nothing is stored.

	With a `query`, Beeper's chat search (all ages) replaces the list and there is no next page.
	"""
	account = session_account()
	client = sync.client_for(account)
	member_client = sync.client_for(account, timeout=UNASSIGNED_TIMEOUT)
	query = (query or "").strip()
	if query:
		raws = beeper_call(list, searched_members(client, member_client, query, INBOX_PAGE_SIZE))
		next_cursor = None
	else:
		page, next_cursor = beeper_call(client.chats, cursor or None, INBOX_PAGE_SIZE)
		merged = [raw for raw in page if is_merged(raw)][:MERGED_SEARCH_LIMIT]
		page = [raw for raw in page if not is_merged(raw) or raw in merged]
		cache: dict = {}
		raws = [member for raw in page for member in beeper_call(expand, member_client, raw, cache)]
	return {"items": unassigned_items(account, raws, bool(cint(include_ignored))), "next_cursor": next_cursor}


def ignored_chats(user: str | None = None) -> set[str]:
	value = frappe.db.get_value("Chat Account", {"user": user or frappe.session.user}, "ignored_chats")
	return set(json.loads(value or "[]"))


@frappe.whitelist(methods=["POST"])
def ignore_chat(beeper_chat_id: str) -> None:
	"""Hides a chat from the caller's unassigned chats; nothing about it is stored but its ID."""
	account = session_account()
	store_ignored(account, ignored_chats(account.user) | {beeper_chat_id})


def store_ignored(account, ignored: set[str]) -> None:
	frappe.db.set_value(
		"Chat Account", account.name, "ignored_chats", json.dumps(sorted(ignored)), update_modified=False
	)


@frappe.whitelist(methods=["POST"])
def unignore_chat(beeper_chat_id: str) -> None:
	account = session_account()
	store_ignored(account, ignored_chats(account.user) - {beeper_chat_id})


@frappe.whitelist()
def unassigned_chats(days: int = 14) -> list[dict]:
	session_account()
	return beeper_call(find_unassigned_chats, None, days)


@frappe.whitelist()
def assign_chat(beeper_chat_id: str, contact: str) -> str:
	account = session_account()
	check_contact(contact)
	raw, cp = direct_chat(sync.client_for(account), beeper_chat_id)
	return assign(account, raw, cp, contact)


def assign(account, raw: dict, cp, contact: str) -> str:
	drop_rows(cp.beeper_chat_id)
	add_row(
		contact,
		{
			"network": cp.network,
			"beeper_chat_id": cp.beeper_chat_id,
			"identifier": cp.username or cp.participant_id,
			"display_name": raw.get("title"),
		},
	)
	chat = sync.ensure_chat(account, raw, cp, contact)
	if not frappe.db.get_value("Chat", chat, "history_complete"):
		frappe.db.set_value("Chat", chat, "history_requested", 1, update_modified=False)
		sync.enqueue("chatsync.sync.import_history", f"chatsync-history-{chat}", chat=chat)
	return chat


@frappe.whitelist()
def create_contact_for_chat(beeper_chat_id: str) -> str:
	raw, cp = direct_chat(sync.client_for(session_account()), beeper_chat_id)
	title = raw.get("title") or cp.name or cp.username or beeper_chat_id
	first_name, _, last_name = title.strip().partition(" ")
	values = {"doctype": "Contact", "first_name": first_name, "last_name": last_name or None}
	if cp.phone:
		values["phone_nos"] = [{"phone": cp.phone}]
	with assignment_writes():
		doc = frappe.get_doc(values).insert()
	assign_chat(beeper_chat_id, doc.name)
	return doc.name


@frappe.whitelist()
def unassign_chat(chat: str) -> None:
	check_chat(chat, write=True)
	beeper_chat_id, contact, network, identifier, title = frappe.db.get_value(
		"Chat", chat, ["beeper_chat_id", "contact", "network", "counterpart_username", "title"]
	)
	identifier = identifier or frappe.db.get_value("Chat", chat, "counterpart_id")
	frappe.db.delete("Chat Message", {"chat": chat})
	frappe.delete_doc("Chat", chat, ignore_permissions=True, force=True)
	if not contact:
		return
	drop_rows(beeper_chat_id, contact)
	add_row(
		contact,
		{
			"network": network,
			"beeper_chat_id": beeper_chat_id,
			"identifier": identifier,
			"display_name": title,
			"excluded": 1,
		},
	)


@frappe.whitelist()
def reassign_chat(chat: str, contact: str) -> str:
	check_chat(chat, write=True)
	check_contact(contact)
	beeper_chat_id = frappe.db.get_value("Chat", chat, "beeper_chat_id")
	unassign_chat(chat)
	return assign_chat(beeper_chat_id, contact)


SEND_COMMAND = "chatsync.api.send_message"
START_COMMAND = "chatsync.api.start_chat"
MESSAGE_FIELDS = [
	"name",
	"sent_at",
	"direction",
	"sender_name",
	"kind",
	"beeper_type",
	"text",
	"transcript",
	"media_url",
	"attachments",
]


def serialize(row: dict) -> dict:
	attachments = messages.attachments_of(row)
	row.pop("media_url", None)
	row.pop("attachments", None)
	return {
		**row,
		"sent_at": str(row["sent_at"]),
		"html": formatting.render(row["text"]),
		"has_media": bool(attachments),
		"attachments": messages.public(attachments),
	}


def newest_first(chats: str | list[str], before: str | None, limit: int) -> list[dict]:
	chats = [chats] if isinstance(chats, str) else chats
	table = frappe.qb.DocType("Chat Message")
	query = (
		frappe.qb.from_(table)
		.select(table.chat, *[table[f] for f in MESSAGE_FIELDS])
		.where(table.chat.isin(chats))
	)
	if before:
		sent_at = frappe.db.get_value("Chat Message", {"name": before, "chat": ["in", chats]}, "sent_at")
		if sent_at:
			query = query.where(
				(table.sent_at < sent_at) | ((table.sent_at == sent_at) & (table.name < before))
			)
	return (
		query.orderby(table.sent_at, order=frappe.qb.desc)
		.orderby(table.name, order=frappe.qb.desc)
		.limit(limit + 1)
		.run(as_dict=True)
	)


@frappe.whitelist()
def get_messages(chat: str, before: str | None = None, limit: int = 200) -> dict:
	"""`before` is the name of the oldest loaded message; older ones come back, oldest first."""
	check_chat(chat)
	limit = max(1, min(int(limit), 500))
	rows = newest_first(chat, before, limit)
	has_more = len(rows) > limit
	rows = rows[:limit][::-1]
	title, network, contact = frappe.db.get_value("Chat", chat, ["title", "network", "contact"])
	return {
		"chat": {
			"title": title,
			"network": network,
			"network_label": networks.label(network),
			"contact": contact,
			"contact_name": contact_name(contact),
		},
		"messages": [serialize(r) for r in rows],
		"has_more": has_more,
		"archive_available": "docarchive" in frappe.get_installed_apps(),
	}


def own_chats(contact: str) -> list[dict]:
	"""The session user's chats with the contact; other users' chats stay out, even for Administrator."""
	chats = frappe.get_list(
		"Chat",
		filters={"contact": contact, "user": frappe.session.user},
		fields=["name", "network", "title", "last_message_at"],
		order_by="last_message_at desc, creation desc",
	)
	if not chats:
		return []
	table = frappe.qb.DocType("Chat Message")
	incoming = dict(
		frappe.qb.from_(table)
		.select(table.chat, frappe.qb.functions("MAX", table.sent_at))
		.where(table.chat.isin([c.name for c in chats]) & (table.direction == "In"))
		.groupby(table.chat)
		.run()
	)
	return [
		{
			"name": c.name,
			"network": c.network,
			"network_label": networks.label(c.network),
			"title": c.title,
			"last_message_at": c.last_message_at,
			"last_incoming_at": incoming.get(c.name),
		}
		for c in chats
	]


def default_chat(chats: list[dict]) -> str | None:
	incoming = [c for c in chats if c["last_incoming_at"]]
	if incoming:
		return max(incoming, key=lambda c: c["last_incoming_at"])["name"]
	return chats[0]["name"] if chats else None


@frappe.whitelist(methods=["GET"])
def get_contact_messages(contact: str, before: str | None = None, limit: int = 200) -> dict:
	"""All of the user's chats with a contact as one conversation; `before` works as in get_messages."""
	check_contact(contact)
	limit = max(1, min(int(limit), 500))
	chats = own_chats(contact)
	by_name = {c["name"]: c for c in chats}
	rows = newest_first(list(by_name), before, limit) if chats else []
	has_more = len(rows) > limit
	result = []
	for row in rows[:limit][::-1]:
		chat = by_name[row["chat"]]
		result.append({**serialize(row), "network": chat["network"], "network_label": chat["network_label"]})
	return {
		"contact": contact,
		"contact_name": contact_name(contact),
		"chats": chats,
		"default_chat": default_chat(chats),
		"messages": result,
		"has_more": has_more,
		"archive_available": "docarchive" in frappe.get_installed_apps(),
	}


def contact_name(contact: str | None) -> str | None:
	if not contact:
		return None
	first, last = frappe.db.get_value("Contact", contact, ["first_name", "last_name"])
	return " ".join(part for part in (first, last) if part) or contact


def has_csrf_token() -> bool:
	"""Frappe skips its CSRF check for sessions without a token (e.g. from POST /api/method/login)."""
	expected = (frappe.session.data or {}).get("csrf_token")
	return bool(expected) and frappe.get_request_header("X-Frappe-CSRF-Token") == expected


def check_browser_session(command: str = SEND_COMMAND, message: str | None = None) -> None:
	"""The request must be the browser's own POST to `command`, so no other endpoint can call it."""
	request = getattr(frappe.local, "request", None)
	if (
		request is None
		or request.method != "POST"
		or frappe.get_request_header("Authorization")
		or frappe.form_dict.get("cmd") != command
		or not has_csrf_token()
	):
		frappe.throw(
			message or frappe._("Sending is only possible from the chat view."), frappe.PermissionError
		)


@frappe.whitelist(methods=["POST"])
def send_message(chat: str, text: str) -> dict:
	"""Text only, browser session only (no API token, no job, no MCP).

	Beeper answers a send with a pendingMessageID only, and its messages carry no field that links
	the pending ID to the final one (linkedMessageID is the reply target). So no provisional row is
	stored: the newest page of the chat is fetched and upserted instead, skipping messages still
	PENDING. A message that is not confirmed yet is stored by the next sync, under its final ID, so
	no duplicate can arise (and sync's remove_vanished would drop a stray pending-ID row anyway).
	"""
	check_browser_session()
	check_chat(chat, write=True)
	text = (text or "").strip()
	if not text:
		frappe.throw(frappe._("The message is empty."))
	client, beeper_chat_id = chat_client(chat)
	beeper_call(client.send_text, beeper_chat_id, text)
	return {"messages": pull_latest(chat, client, beeper_chat_id)}


def chat_client(chat: str):
	beeper_chat_id, account = frappe.db.get_value("Chat", chat, ["beeper_chat_id", "account"])
	return sync.client_for(frappe.get_doc("Chat Account", account)), beeper_chat_id


def pull_latest(chat: str, client, beeper_chat_id: str) -> list[dict]:
	items, _cursor = beeper_call(client.messages, beeper_chat_id)
	names = []
	for raw in items:
		if (raw.get("sendStatus") or {}).get("status") == "PENDING":
			continue
		if name := messages.upsert(chat, raw):
			names.append(name)
	last = frappe.db.get_value("Chat Message", {"chat": chat}, "sent_at", order_by="sent_at desc")
	frappe.db.set_value("Chat", chat, "last_message_at", last, update_modified=False)
	rows = frappe.get_all(
		"Chat Message", filters={"name": ["in", names]}, fields=MESSAGE_FIELDS, order_by="sent_at"
	)
	return [serialize(r) for r in rows]


@frappe.whitelist(methods=["POST"])
def refresh_messages(chat: str) -> dict:
	"""Read-only: fetch the newest page from Beeper and store it (used after a send to catch the confirmation)."""
	check_chat(chat, write=True)
	client, beeper_chat_id = chat_client(chat)
	return {"messages": pull_latest(chat, client, beeper_chat_id)}


def contact_phones(contact: str) -> list[str]:
	region = frappe.db.get_single_value("Chat Sync Settings", "phone_region") or "DE"
	phones = frappe.get_all(
		"Contact Phone", filters={"parenttype": "Contact", "parent": contact}, pluck="phone", order_by="idx"
	)
	return list(dict.fromkeys(e164 for phone in phones if phone and (e164 := normalize(phone, region))))


def phone_account(raw: dict) -> dict | None:
	network = networks.key((raw.get("bridge") or {}).get("type") or raw.get("accountID"))
	if network not in PHONE_NETWORKS or raw.get("status") == "disabled":
		return None
	user = raw.get("user") or {}
	return {
		"account_id": raw["accountID"],
		"network": network,
		"network_label": networks.label(network),
		"name": user.get("fullName") or user.get("phoneNumber") or user.get("username"),
		"phone": user.get("phoneNumber"),
	}


@frappe.whitelist(methods=["GET"])
def chat_start_options(contact: str) -> dict:
	"""The user's Beeper accounts that reach people by phone number, and the contact's numbers."""
	account = session_account()
	check_contact(contact)
	accounts = beeper_call(sync.client_for(account).accounts)
	return {
		"accounts": [found for raw in accounts if (found := phone_account(raw))],
		"phones": contact_phones(contact),
	}


@frappe.whitelist(methods=["POST"])
def start_chat(contact: str, account_id: str, phone: str) -> str:
	"""Opens a direct chat with one of the contact's numbers and assigns it; sends nothing."""
	check_browser_session(START_COMMAND, frappe._("Only possible in a browser session."))
	account = session_account()
	check_contact(contact)
	if phone not in contact_phones(contact):
		frappe.throw(frappe._("{0} is not a phone number of this contact.").format(escape_html(phone)))
	client = sync.client_for(account)
	user = {"phoneNumber": phone, "fullName": contact_name(contact)}
	try:
		raw = client.start_chat(account_id, user)
	except BeeperError as e:
		frappe.throw(
			frappe._(
				"Beeper could not start the chat ({0}). Is the number registered on this network?"
			).format(e.message or e.status)
		)
	if is_merged(raw):
		raw = started_member(client, raw, account_id, phone)
	cp = counterpart(raw)
	if not cp:
		raw, cp = direct_chat(client, raw["id"])
	stored = frappe.db.get_value(
		"Chat",
		{"account": account.name, "beeper_chat_id": cp.beeper_chat_id},
		["name", "contact"],
		as_dict=True,
	)
	if stored and stored.contact == contact:
		return stored.name
	if stored and stored.contact:
		frappe.throw(
			frappe._("This chat already belongs to another contact ({0}).").format(
				escape_html(contact_name(stored.contact))
			)
		)
	return assign(account, raw, cp, contact)


def started_member(client, raw: dict, account_id: str, phone: str) -> dict:
	"""Beeper may answer a start with the merged chat the new chat joined; pick that member."""
	members = beeper_call(expand, client, raw)
	for member in members:
		if member.get("accountID") == account_id:
			return member
	for member in members:
		if (cp := counterpart(member)) and cp.phone == phone:
			return member
	frappe.throw(frappe._("This is a merged Beeper chat, pick one of its chats instead."))


@frappe.whitelist(methods=["GET"])
def preview_chat(beeper_chat_id: str, limit: int = 20) -> dict:
	"""The newest messages of an unassigned direct chat, fetched live; nothing is stored."""
	account = session_account()
	limit = max(1, min(int(limit), PREVIEW_LIMIT))
	if frappe.db.exists("Chat", {"account": account.name, "beeper_chat_id": beeper_chat_id}):
		frappe.throw(frappe._("This chat is already assigned, open it in the chat view."))
	client = sync.client_for(account)
	raw, cp = direct_chat(client, beeper_chat_id)
	items, _cursor = beeper_call(client.messages, beeper_chat_id)
	shown = sorted(filter(messages.is_shown, items), key=lambda m: messages.sort_order(m.get("sortKey")))
	return {
		"beeper_chat_id": beeper_chat_id,
		"title": raw.get("title"),
		"network": cp.network,
		"network_label": networks.label(cp.network),
		"messages": [messages.preview(m) for m in shown[-limit:]],
	}


@frappe.whitelist(methods=["GET"])
def get_conversations() -> list[dict]:
	"""The user's conversations, newest first: one per contact (all its chats), else one per chat."""
	chats = frappe.get_list(
		"Chat",
		filters={"user": frappe.session.user},
		fields=["name", "network", "title", "contact", "last_message_at"],
		limit_page_length=0,
	)
	groups: dict[str, list] = {}
	for chat in chats:
		groups.setdefault(chat.contact or chat.name, []).append(chat)
	names = contact_names([c.contact for c in chats if c.contact])
	table = frappe.qb.DocType("Chat Message")
	rows = []
	for key, members in groups.items():
		last = (
			frappe.qb.from_(table)
			.select(table.sent_at, table.direction, table.kind, table.text, table.transcript)
			.where(table.chat.isin([c.name for c in members]))
			.orderby(table.sent_at, order=frappe.qb.desc)
			.limit(1)
			.run(as_dict=True)
		)
		last = last[0] if last else {}
		contact = members[0].contact
		rows.append(
			{
				"key": key,
				"contact": contact,
				"chat": None if contact else key,
				"title": names.get(contact) or members[0].title or key,
				"networks": sorted({networks.label(c.network) for c in members}),
				"last_message_at": last.get("sent_at")
				or max((c.last_message_at for c in members if c.last_message_at), default=None),
				"direction": last.get("direction"),
				"preview": messages.summary_text(last) if last else "",
			}
		)
	return sorted(rows, key=lambda r: str(r["last_message_at"] or ""), reverse=True)


def contact_names(contacts: list[str]) -> dict[str, str]:
	rows = frappe.get_all(
		"Contact",
		filters={"name": ["in", list(set(contacts)) or [""]]},
		fields=["name", "first_name", "last_name"],
	)
	return {r.name: " ".join(p for p in (r.first_name, r.last_name) if p) or r.name for r in rows}
