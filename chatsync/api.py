import json
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import urlencode

import frappe
from frappe.utils import cint, escape_html, now_datetime

from chatsync import formatting, messages, networks, sync
from chatsync.beeper import BeeperError
from chatsync.extensions import group_parties
from chatsync.matching import (
	counterpart,
	group_members,
	is_group,
	match_chat,
	member_count,
	name_index,
	name_suggestion,
	network_of,
	phone_contacts,
	phone_index,
)
from chatsync.merges import expand, is_merged
from chatsync.phone import normalize

UNASSIGNED_TIMEOUT = 10
PHONE_NETWORKS = {"whatsapp", "signal"}
PREVIEW_LIMIT = 100
MERGED_SEARCH_LIMIT = 10
INBOX_PAGE_SIZE = 50
AVATAR_ENDPOINT = "/api/method/chatsync.media.avatar"
AVATAR_SCHEMES = ("mxc://", "localmxc://", "file://")


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
	return direct(beeper_call(client.chat, beeper_chat_id))


def direct(raw: dict):
	if is_merged(raw):
		frappe.throw(frappe._("This is a merged Beeper chat, pick one of its chats instead."))
	cp = counterpart(raw)
	if not cp:
		frappe.throw(frappe._("Only direct chats can be assigned"))
	return raw, cp


def listed_chat(client, beeper_chat_id: str):
	"""A direct chat with its counterpart, or a group chat (counterpart None); merged chats are refused."""
	raw = beeper_call(client.chat, beeper_chat_id)
	return (raw, None) if is_group(raw) else direct(raw)


def group_chat(client, beeper_chat_id: str) -> dict:
	raw = beeper_call(client.chat, beeper_chat_id)
	if not is_group(raw):
		frappe.throw(frappe._("Only group chats can be assigned to parties."))
	return beeper_call(sync.with_all_participants, client, raw)


def summary(raw: dict, cp) -> dict:
	return {
		"beeper_chat_id": cp.beeper_chat_id,
		"network": cp.network,
		"title": raw.get("title"),
		"phone": cp.phone,
		"username": cp.username,
		"last_activity": raw.get("lastActivity"),
		"avatar_url": avatar_endpoint(beeper_chat_id=cp.beeper_chat_id) if avatar_source(raw) else None,
	}


def avatar_source(raw: dict) -> str | None:
	"""The counterpart's Beeper picture, else the chat's, if the Beeper Server can serve it."""
	cp = counterpart(raw)
	for url in (cp and cp.image, raw.get("imgURL")):
		if url and url.startswith(AVATAR_SCHEMES):
			return url
	return None


def avatar_endpoint(**args) -> str:
	return f"{AVATAR_ENDPOINT}?{urlencode(args)}"


def newest_chat(chats: list[dict]) -> str | None:
	"""The chat whose Beeper picture stands for the contact: the same one in every view."""
	if not chats:
		return None
	return max(chats, key=lambda c: (str(c["last_message_at"] or ""), str(c["creation"]), c["name"]))["name"]


def avatar_url(contact: str | None, chat: str | None, image: str | None = None) -> str | None:
	"""The contact's own picture first, else the Beeper picture of `chat`, streamed on request."""
	if contact and not image:
		image = frappe.db.get_value("Contact", contact, "image")
	return image or (avatar_endpoint(chat=chat) if chat else None)


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


def searched_members(client, member_client, query: str, limit: int = 20, kind: str = "single"):
	"""Beeper's search hits (any age) with merged chats replaced by their members."""
	cache: dict = {}
	merged_left = MERGED_SEARCH_LIMIT
	for found in client.search_chats(query, limit, kind):
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
	"""The direct chats among `raws` that no contact takes, with what could be assigned instead, and the
	group chats not stored yet, with the members' contacts and the parties they suggest."""
	index, names = phone_index(), name_index()
	ignored = ignored_chats(account.user)
	excluded = excluded_groups(account.user)
	stored = set(
		frappe.get_all("Chat", filters={"account": account.name, "is_group": 1}, pluck="beeper_chat_id")
	)
	result = {}
	for raw in raws:
		if is_group(raw):
			hidden = raw["id"] in ignored or raw["id"] in excluded
			if raw["id"] in result or raw["id"] in stored or (hidden and not include_ignored):
				continue
			result[raw["id"]] = group_item(account, raw, index, names, hidden)
			continue
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
			"archived": bool(raw.get("isArchived")),
			"needs_reply": waits_for_reply(raw),
			"suggested_contact": suggested,
			"suggested_contact_name": contact_name(suggested),
		}
	return list(result.values())


def group_item(account, raw: dict, index: dict, names: dict, hidden: bool) -> dict:
	members = group_members(account.user, raw, index, names)
	contacts = [m["contact"] for m in members if m["contact"]]
	for member in members:
		member["contact_name"] = contact_name(member["contact"])
	return {
		"beeper_chat_id": raw["id"],
		"network": network_of(raw),
		"network_label": networks.label(network_of(raw)),
		"title": raw.get("title"),
		"phone": None,
		"username": None,
		"last_activity": raw.get("lastActivity"),
		"avatar_url": avatar_endpoint(beeper_chat_id=raw["id"]) if avatar_source(raw) else None,
		"is_group": True,
		"member_count": member_count(raw),
		"members": members,
		"suggested_parties": suggested_parties(contacts),
		"ambiguous": False,
		"existing_contact": None,
		"ignored": hidden,
		"archived": bool(raw.get("isArchived")),
		"needs_reply": waits_for_reply(raw),
		"suggested_contact": None,
		"suggested_contact_name": None,
	}


def suggested_parties(contacts: list[str]) -> list[dict]:
	return [
		{"party_type": party_type, "party": party, "title": party_title(party_type, party)}
		for party_type, party in group_parties(contacts)
		if frappe.db.exists(party_type, party) and frappe.has_permission(party_type, "read", doc=party)
	]


def party_title(party_type: str, party: str) -> str:
	field = frappe.get_meta(party_type).title_field
	return (field and frappe.db.get_value(party_type, party, field)) or party


def waits_for_reply(raw: dict) -> bool:
	"""The last message is the other person's; chats fetched one by one carry no preview, only unread state."""
	preview = raw.get("preview")
	if preview:
		return not preview.get("isSender")
	return bool(raw.get("unreadCount") or raw.get("isMarkedUnread"))


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
		raws = beeper_call(list, searched_members(client, member_client, query, INBOX_PAGE_SIZE, "any"))
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


def excluded_groups(user: str | None = None) -> set[str]:
	"""Group chats that were unassigned; they are offered again only among ignored chats."""
	value = frappe.db.get_value("Chat Account", {"user": user or frappe.session.user}, "excluded_groups")
	return set(json.loads(value or "[]"))


def store_excluded(account: str, excluded: set[str]) -> None:
	frappe.db.set_value(
		"Chat Account", account, "excluded_groups", json.dumps(sorted(excluded)), update_modified=False
	)


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
	request_history(chat)
	return chat


ASSIGN_GROUP_COMMAND = "chatsync.api.assign_group"


@frappe.whitelist(methods=["POST"])
def assign_group(
	beeper_chat_id: str, parties: str | list | None = None, contacts: str | list | None = None
) -> str:
	"""Stores a group chat by the user's explicit choice: attached to `parties` ([{party_type, party}])
	and linking the members whose contacts are in `contacts`; imports its history.

	Browser session only (no API token, no MCP): a group carries many third parties' messages, so it
	is stored only after the user looked at it. Members' contacts get no Chat Identifier rows, so
	matching of direct chats is unaffected.
	"""
	check_browser_session(ASSIGN_GROUP_COMMAND, frappe._("Only possible in a browser session."))
	account = session_account()
	parties = checked_parties(frappe.parse_json(parties) or [])
	contacts = list(dict.fromkeys(frappe.parse_json(contacts) or []))
	if not parties and not contacts:
		frappe.throw(frappe._("Choose at least one party or member."))
	for contact in contacts:
		check_contact(contact)
	raw = group_chat(sync.client_for(account), beeper_chat_id)
	members = group_members(account.user, raw, phone_index(), name_index())
	strangers = set(contacts) - {m["contact"] for m in members}
	if strangers:
		frappe.throw(
			frappe._("Not a member of this group: {0}").format(
				escape_html(", ".join(contact_name(c) for c in sorted(strangers)))
			)
		)
	for member in members:
		if member["contact"] not in contacts:
			member["contact"] = None
	chat = sync.ensure_group(account, raw, parties, members)
	store_excluded(account.name, excluded_groups(account.user) - {beeper_chat_id})
	request_history(chat)
	return chat


def checked_parties(parties: list) -> list[dict]:
	result = {}
	for party in parties:
		party_type, name = party.get("party_type"), party.get("party")
		if not (party_type and name and frappe.db.exists("DocType", party_type)):
			frappe.throw(frappe._("Invalid party: {0}").format(escape_html(f"{party_type} {name}")))
		if not frappe.db.exists(party_type, name):
			frappe.throw(frappe._("{0} {1} not found").format(escape_html(party_type), escape_html(name)))
		if not frappe.has_permission(party_type, "read", doc=name):
			frappe.throw(
				frappe._("No permission for {0} {1}").format(escape_html(party_type), escape_html(name)),
				frappe.PermissionError,
			)
		result[(party_type, name)] = {"party_type": party_type, "party": name}
	return list(result.values())


def request_history(chat: str) -> None:
	if not frappe.db.get_value("Chat", chat, "history_complete"):
		frappe.db.set_value("Chat", chat, "history_requested", 1, update_modified=False)
		sync.enqueue("chatsync.sync.import_history", f"chatsync-history-{chat}", chat=chat)


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
	beeper_chat_id, contact, network, identifier, title, group, account = frappe.db.get_value(
		"Chat",
		chat,
		["beeper_chat_id", "contact", "network", "counterpart_username", "title", "is_group", "account"],
	)
	identifier = identifier or frappe.db.get_value("Chat", chat, "counterpart_id")
	frappe.db.delete("Chat Message", {"chat": chat})
	frappe.delete_doc("Chat", chat, ignore_permissions=True, force=True)
	if group:
		user = frappe.db.get_value("Chat Account", account, "user")
		store_excluded(account, excluded_groups(user) | {beeper_chat_id})
		return
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
	beeper_chat_id, group = frappe.db.get_value("Chat", chat, ["beeper_chat_id", "is_group"])
	if group:
		frappe.throw(frappe._("A group chat belongs to parties, not to one contact."))
	unassign_chat(chat)
	return assign_chat(beeper_chat_id, contact)


SEND_COMMAND = "chatsync.api.send_message"
START_COMMAND = "chatsync.api.start_chat"
MESSAGE_FIELDS = [
	"name",
	"sent_at",
	"direction",
	"sender_name",
	"sender_id",
	"kind",
	"beeper_type",
	"text",
	"transcript",
	"media_url",
	"attachments",
	"reactions",
]


def serialize(row: dict) -> dict:
	attachments = messages.attachments_of(row)
	row.pop("media_url", None)
	row.pop("attachments", None)
	return {
		**row,
		"reactions": messages.grouped_reactions(row.get("reactions")),
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
	title, network, contact, archived, owner, group = frappe.db.get_value(
		"Chat", chat, ["title", "network", "contact", "beeper_archived", "user", "is_group"]
	)
	return {
		"chat": {
			**(group_details(chat) if group else {"is_group": False}),
			"archived": bool(archived),
			"own": owner == frappe.session.user,
			"title": title,
			"network": network,
			"network_label": networks.label(network),
			"contact": contact,
			"contact_name": contact_name(contact),
			"avatar_url": avatar_url(contact, chat),
		},
		"messages": [serialize(r) for r in rows],
		"has_more": has_more,
		"archive_available": "docarchive" in frappe.get_installed_apps(),
	}


def group_details(chat: str) -> dict:
	doc = frappe.get_doc("Chat", chat)
	contacts = [m.contact for m in doc.members if m.contact]
	names, images = contact_names(contacts), contact_images(contacts)
	return {
		"is_group": True,
		"members": [
			{
				"participant_id": m.participant_id,
				"display_name": m.display_name,
				"contact": m.contact,
				"contact_name": names.get(m.contact),
				"avatar_url": images.get(m.contact),
			}
			for m in doc.members
		],
		"parties": [
			{"party_type": p.party_type, "party": p.party, "title": party_title(p.party_type, p.party)}
			for p in doc.parties
		],
	}


def own_chats(contact: str) -> list[dict]:
	"""The session user's chats with the contact; other users' chats stay out, even for Administrator."""
	chats = frappe.get_list(
		"Chat",
		filters={"contact": contact, "user": frappe.session.user},
		fields=["name", "network", "title", "last_message_at", "creation", "beeper_archived"],
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
			"creation": c.creation,
			"archived": bool(c.beeper_archived),
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
		"avatar_url": avatar_url(contact, newest_chat(chats)),
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
			message or frappe._("Sending is only possible from the chat window."), frappe.PermissionError
		)


@frappe.whitelist(methods=["POST"])
def send_message(chat: str, text: str) -> dict:
	"""Text only, browser session only (no API token, no job, no MCP).

	Returns as soon as Beeper accepted the message, with its pendingMessageID. Beeper's messages
	carry no field that links that ID to the final one (linkedMessageID is the reply target), so
	nothing is stored here: the client polls refresh_messages, which stores the newest page and
	skips messages still PENDING. A message not confirmed by then is stored by the next sync under
	its final ID, so no duplicate can arise.
	"""
	check_browser_session()
	check_chat(chat, write=True)
	text = (text or "").strip()
	if not text:
		frappe.throw(frappe._("The message is empty."))
	client, beeper_chat_id = chat_client(chat)
	sent = beeper_call(client.send_text, beeper_chat_id, text)
	return {"pending_message_id": (sent or {}).get("pendingMessageID")}


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
	sync.update_activity(chat)
	rows = frappe.get_all(
		"Chat Message", filters={"name": ["in", names]}, fields=MESSAGE_FIELDS, order_by="sent_at"
	)
	return [serialize(r) for r in rows]


@frappe.whitelist(methods=["POST"])
def refresh_messages(chat: str) -> dict:
	"""Fetches the newest page from Beeper and stores it; the client polls it after a send."""
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
	raw, cp = listed_chat(client, beeper_chat_id)
	items, _cursor = beeper_call(client.messages, beeper_chat_id)
	shown = sorted(filter(messages.is_shown, items), key=lambda m: messages.sort_order(m.get("sortKey")))
	return {
		"beeper_chat_id": beeper_chat_id,
		"title": raw.get("title"),
		"is_group": cp is None,
		"network": network_of(raw),
		"network_label": networks.label(network_of(raw)),
		"avatar_url": avatar_endpoint(beeper_chat_id=beeper_chat_id) if avatar_source(raw) else None,
		"messages": [messages.preview(m) for m in shown[-limit:]],
	}


@frappe.whitelist(methods=["GET"])
def get_conversations() -> list[dict]:
	"""The user's conversations, newest first: one per contact (all its chats), else one per chat."""
	chats = frappe.get_list(
		"Chat",
		filters={"user": frappe.session.user},
		fields=[
			"name",
			"network",
			"title",
			"contact",
			"last_message_at",
			"creation",
			"beeper_archived",
			"is_group",
		],
		limit_page_length=0,
	)
	groups: dict[str, list] = {}
	for chat in chats:
		groups.setdefault(chat.contact or chat.name, []).append(chat)
	names = contact_names([c.contact for c in chats if c.contact])
	images = contact_images([c.contact for c in chats if c.contact])
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
				"is_group": bool(members[0].is_group),
				"title": names.get(contact) or members[0].title or key,
				"networks": sorted({networks.label(c.network) for c in members}),
				"last_message_at": last.get("sent_at")
				or max((c.last_message_at for c in members if c.last_message_at), default=None),
				"direction": last.get("direction"),
				"preview": messages.summary_text(last) if last else "",
				"avatar_url": avatar_url(None, newest_chat(members), images.get(contact)),
				"archived": all(c.beeper_archived for c in members),
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


def contact_images(contacts: list[str]) -> dict[str, str]:
	rows = frappe.get_all(
		"Contact",
		filters={"name": ["in", list(set(contacts)) or [""]], "image": ["is", "set"]},
		fields=["name", "image"],
	)
	return {r.name: r.image for r in rows}


DONE_COMMAND = "chatsync.api.set_chat_done"
REACT_COMMAND = "chatsync.api.react"
REACTION_KEY_LIMIT = 64
OPEN_CHAT_FIELDS = [
	"name",
	"contact",
	"network",
	"title",
	"last_message_at",
	"last_incoming_at",
	"last_outgoing_at",
	"unread_count",
	"creation",
	"is_group",
]


def run_hooks(name: str, **kwargs) -> None:
	for method in frappe.get_hooks(name):
		frappe.get_attr(method)(**kwargs)


@frappe.whitelist(methods=["POST"])
def set_chat_done(chat: str | None = None, beeper_chat_id: str | None = None, done: int = 1) -> dict:
	"""Archives a chat in Beeper ("done"), or with done=0 moves it back to the inbox.

	Browser session only: it changes the user's messenger. Beeper archives a merged chat
	together with the member that is archived, so the member's own ID is used. Takes a stored `chat` or an unassigned direct chat.
	"""
	check_browser_session(DONE_COMMAND, frappe._("Only possible in a browser session."))
	if bool(chat) == bool(beeper_chat_id):
		frappe.throw(frappe._("Give either a chat or a Beeper chat ID."))
	done = bool(cint(done))
	if chat:
		check_chat(chat, write=True)
		client, beeper_chat_id = chat_client(chat)
		account = frappe.db.get_value("Chat", chat, "account")
	else:
		doc = session_account()
		account = doc.name
		client = sync.client_for(doc)
		listed_chat(client, beeper_chat_id)
	beeper_call(client.archive, beeper_chat_id, done)
	stored = frappe.db.get_value("Chat", {"account": account, "beeper_chat_id": beeper_chat_id})
	if stored:
		frappe.db.set_value("Chat", stored, "beeper_archived", int(done), update_modified=False)
	user = frappe.db.get_value("Chat Account", account, "user")
	run_hooks("chatsync_chat_done", user=user, beeper_chat_id=beeper_chat_id, done=done)
	return {"chat": chat, "beeper_chat_id": beeper_chat_id, "done": done}


def waiting(last_incoming_at, last_outgoing_at) -> bool:
	"""The newest message in any of a contact's chats came in: an answer in another network counts."""
	return bool(last_incoming_at) and (not last_outgoing_at or last_incoming_at > last_outgoing_at)


def open_filters(user: str) -> dict | None:
	accounts = frappe.get_all("Chat Account", filters={"user": user, "enabled": 1}, pluck="name")
	if not accounts:
		return None
	return {"user": user, "account": ["in", accounts], "beeper_archived": 0}


def open_chat_counts(user: str | None = None) -> dict:
	"""Open rows (contacts, else chats) and those waiting for a reply, in one query: for badges."""
	filters = open_filters(user or frappe.session.user)
	if not filters:
		return {"open": 0, "waiting": 0}
	table = frappe.qb.DocType("Chat")
	key = frappe.qb.functions("COALESCE", table.contact, table.name)
	rows = (
		frappe.qb.from_(table)
		.select(
			frappe.qb.functions("MAX", table.last_incoming_at),
			frappe.qb.functions("MAX", table.last_outgoing_at),
		)
		.where(
			(table.user == filters["user"])
			& table.account.isin(filters["account"][1])
			& (table.beeper_archived == 0)
		)
		.groupby(key)
		.run()
	)
	return {"open": len(rows), "waiting": sum(waiting(i, o) for i, o in rows)}


def open_assigned_chats(user: str | None = None, limit: int | None = None) -> list[dict]:
	"""The user's stored chats not archived in Beeper, one row per contact; waiting ones first.
	Previews are read for the returned rows only."""
	filters = open_filters(user or frappe.session.user)
	if not filters:
		return []
	chats = frappe.get_all(
		"Chat", filters=filters, fields=OPEN_CHAT_FIELDS, order_by="last_message_at desc, creation desc"
	)
	groups: dict[str, list] = {}
	for chat in chats:
		groups.setdefault(chat.contact or chat.name, []).append(chat)
	contacts = [c.contact for c in chats if c.contact]
	names, images = contact_names(contacts), contact_images(contacts)
	rows = [open_row(members, names, images) for members in groups.values()]
	rows.sort(key=lambda r: (r["needs_reply"], str(r["last_activity"] or "")), reverse=True)
	rows = rows[:limit] if limit else rows
	for row in rows:
		last = newest_message(row["chats"])
		row["preview"] = messages.summary_text(last) if last else ""
		row["direction"] = last.get("direction") if last else None
	return rows


def open_row(members: list, names: dict, images: dict) -> dict:
	incoming = [c for c in members if c.last_incoming_at]
	reply_to = max(incoming, key=lambda c: c.last_incoming_at) if incoming else members[0]
	last_out = max((c.last_outgoing_at for c in members if c.last_outgoing_at), default=None)
	contact = members[0].contact
	return {
		"chat": reply_to.name,
		"chats": [c.name for c in members],
		"contact": contact,
		"is_group": bool(members[0].is_group),
		"contact_name": names.get(contact) or members[0].title or members[0].name,
		"networks": sorted({networks.label(c.network) for c in members}),
		"last_activity": max((c.last_message_at for c in members if c.last_message_at), default=None),
		"last_incoming_at": reply_to.last_incoming_at,
		"needs_reply": waiting(reply_to.last_incoming_at, last_out),
		"unread_count": sum(c.unread_count or 0 for c in members),
		"avatar_url": avatar_url(None, newest_chat(members), images.get(contact)),
	}


def newest_message(chats: list[str]) -> dict | None:
	table = frappe.qb.DocType("Chat Message")
	rows = (
		frappe.qb.from_(table)
		.select(table.direction, table.kind, table.text, table.transcript)
		.where(table.chat.isin(chats))
		.orderby(table.sent_at, order=frappe.qb.desc)
		.limit(1)
		.run(as_dict=True)
	)
	return rows[0] if rows else None


@frappe.whitelist(methods=["POST"])
def react(message: str, key: str, remove: int = 0) -> dict:
	"""Adds (or removes) the user's reaction; browser session only, like sending: the other person sees it."""
	check_browser_session(REACT_COMMAND, frappe._("Reacting is only possible from the chat window."))
	found = frappe.db.get_value("Chat Message", message, ["chat", "beeper_message_id", "reactions"])
	if not found:
		frappe.throw(
			frappe._("No permission for Chat Message {0}").format(escape_html(message)),
			frappe.PermissionError,
		)
	chat, beeper_message_id, stored = found
	check_chat(chat, write=True)
	key = (key or "").strip()
	if not key or len(key) > REACTION_KEY_LIMIT:
		frappe.throw(frappe._("Invalid reaction."))
	client, beeper_chat_id = chat_client(chat)
	remove = bool(cint(remove))
	call = client.unreact if remove else client.react
	beeper_call(call, beeper_chat_id, beeper_message_id, key)
	reactions = [r for r in json.loads(stored or "[]") if not (r.get("is_self") and r["key"] == key)]
	if not remove:
		reactions.append({"key": key, "participant_id": None, "is_self": True})
	value = json.dumps(reactions, ensure_ascii=False) if reactions else None
	frappe.db.set_value("Chat Message", message, "reactions", value)
	return {"reactions": messages.grouped_reactions(value)}
