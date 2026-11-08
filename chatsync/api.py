import json
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from chatsync import formatting, messages, sync
from chatsync.beeper import BeeperError
from chatsync.matching import counterpart, match_chat, phone_contacts, phone_index

UNASSIGNED_TIMEOUT = 5


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


@frappe.whitelist()
def search_chats(query: str) -> list[dict]:
	client = sync.client_for(session_account())
	result = []
	for raw in beeper_call(client.search_chats, query):
		if cp := counterpart(raw):
			result.append(summary(raw, cp))
	return result


def find_unassigned_chats(user: str | None = None, days: int = 14) -> list[dict]:
	"""Direct chats active in the last `days` that no contact takes. Raises BeeperError."""
	name = frappe.db.get_value("Chat Account", {"user": user or frappe.session.user, "enabled": 1})
	if not name:
		return []
	account = frappe.get_doc("Chat Account", name)
	client = sync.client_for(account, timeout=UNASSIGNED_TIMEOUT)
	threshold = now_datetime() - timedelta(days=int(days))
	index = phone_index()
	ignored = ignored_chats(account.user)
	result = []
	for raw in sync.walk_chats(client, threshold):
		cp = counterpart(raw)
		if not cp or cp.beeper_chat_id in ignored:
			continue
		found = match_chat(account, cp, index=index)
		if found.contact or found.excluded:
			continue
		existing = phone_contacts(account.user, cp.phone, index) if cp.phone else []
		result.append(
			{
				**summary(raw, cp),
				"ambiguous": found.ambiguous,
				"existing_contact": existing[0] if len(existing) == 1 else None,
			}
		)
	return result


def ignored_chats(user: str | None = None) -> set[str]:
	value = frappe.db.get_value("Chat Account", {"user": user or frappe.session.user}, "ignored_chats")
	return set(json.loads(value or "[]"))


@frappe.whitelist(methods=["POST"])
def ignore_chat(beeper_chat_id: str) -> None:
	"""Hides a chat from the caller's unassigned chats; nothing about it is stored but its ID."""
	account = session_account()
	ignored = sorted(ignored_chats(account.user) | {beeper_chat_id})
	frappe.db.set_value(
		"Chat Account", account.name, "ignored_chats", json.dumps(ignored), update_modified=False
	)


@frappe.whitelist()
def unassigned_chats(days: int = 14) -> list[dict]:
	session_account()
	return beeper_call(find_unassigned_chats, None, days)


@frappe.whitelist()
def assign_chat(beeper_chat_id: str, contact: str) -> str:
	account = session_account()
	check_contact(contact)
	raw, cp = direct_chat(sync.client_for(account), beeper_chat_id)
	drop_rows(beeper_chat_id)
	add_row(
		contact,
		{
			"network": cp.network,
			"beeper_chat_id": beeper_chat_id,
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
MESSAGE_FIELDS = ["name", "sent_at", "direction", "sender_name", "kind", "text", "transcript", "media_url"]


def serialize(row: dict) -> dict:
	media_url = row.pop("media_url", None)
	return {
		**row,
		"sent_at": str(row["sent_at"]),
		"html": formatting.render(row["text"]),
		"has_media": bool(media_url),
	}


def newest_first(chat: str, before: str | None, limit: int) -> list[dict]:
	table = frappe.qb.DocType("Chat Message")
	query = frappe.qb.from_(table).select(*[table[f] for f in MESSAGE_FIELDS]).where(table.chat == chat)
	if before:
		sent_at = frappe.db.get_value("Chat Message", {"name": before, "chat": chat}, "sent_at")
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
			"contact": contact,
			"contact_name": contact_name(contact),
		},
		"messages": [serialize(r) for r in rows],
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


def check_browser_session() -> None:
	request = getattr(frappe.local, "request", None)
	if (
		request is None
		or request.method != "POST"
		or frappe.get_request_header("Authorization")
		or frappe.form_dict.get("cmd") != SEND_COMMAND
		or not has_csrf_token()
	):
		frappe.throw(frappe._("Sending is only possible from the chat view."), frappe.PermissionError)


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
