from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from chatsync import messages, sync
from chatsync.beeper import BeeperError
from chatsync.matching import counterpart, match, phone_index


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


def check_chat(chat: str) -> None:
	if not frappe.has_permission("Chat", "write", doc=chat):
		frappe.throw(frappe._("No permission for Chat {0}").format(chat), frappe.PermissionError)


def drop_rows(contact_doc, beeper_chat_id: str) -> None:
	contact_doc.chat_identifiers = [
		r for r in contact_doc.chat_identifiers if r.beeper_chat_id != beeper_chat_id
	]


@frappe.whitelist()
def search_chats(query: str) -> list[dict]:
	client = sync.client_for(session_account())
	result = []
	for raw in beeper_call(client.search_chats, query):
		if cp := counterpart(raw):
			result.append(summary(raw, cp))
	return result


@frappe.whitelist()
def unassigned_chats(days: int = 14) -> list[dict]:
	account = session_account()
	client = sync.client_for(account)
	threshold = now_datetime() - timedelta(days=int(days))
	index = phone_index()
	result = []
	try:
		for raw in sync.walk_chats(client, threshold):
			cp = counterpart(raw)
			if not cp:
				continue
			found = match(account.user, cp, index=index)
			if found.contact or found.excluded:
				continue
			result.append({**summary(raw, cp), "ambiguous": found.ambiguous})
	except BeeperError as e:
		frappe.throw(str(e))
	return result


@frappe.whitelist()
def assign_chat(beeper_chat_id: str, contact: str) -> str:
	account = session_account()
	check_contact(contact)
	raw, cp = direct_chat(sync.client_for(account), beeper_chat_id)
	identifier = cp.username or cp.participant_id
	with assignment_writes():
		frappe.db.delete("Chat Identifier", {"parenttype": "Contact", "beeper_chat_id": beeper_chat_id})
		doc = frappe.get_doc("Contact", contact)
		drop_rows(doc, beeper_chat_id)
		doc.append(
			"chat_identifiers",
			{
				"network": cp.network,
				"beeper_chat_id": beeper_chat_id,
				"identifier": identifier,
				"display_name": raw.get("title"),
			},
		)
		doc.save(ignore_permissions=True)
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
	check_chat(chat)
	beeper_chat_id, contact, network, identifier, title = frappe.db.get_value(
		"Chat", chat, ["beeper_chat_id", "contact", "network", "counterpart_username", "title"]
	)
	identifier = identifier or frappe.db.get_value("Chat", chat, "counterpart_id")
	frappe.db.delete("Chat Message", {"chat": chat})
	frappe.delete_doc("Chat", chat, ignore_permissions=True, force=True)
	if not contact:
		return
	with assignment_writes():
		doc = frappe.get_doc("Contact", contact)
		drop_rows(doc, beeper_chat_id)
		doc.append(
			"chat_identifiers",
			{
				"network": network,
				"beeper_chat_id": beeper_chat_id,
				"identifier": identifier,
				"display_name": title,
				"excluded": 1,
			},
		)
		doc.save(ignore_permissions=True)


@frappe.whitelist()
def reassign_chat(chat: str, contact: str) -> None:
	check_chat(chat)
	check_contact(contact)
	beeper_chat_id = frappe.db.get_value("Chat", chat, "beeper_chat_id")
	unassign_chat(chat)
	assign_chat(beeper_chat_id, contact)


MESSAGE_FIELDS = ["name", "sent_at", "direction", "sender_name", "kind", "text", "transcript", "media_url"]


def serialize(row: dict) -> dict:
	media_url = row.pop("media_url", None)
	return {**row, "sent_at": str(row["sent_at"]), "has_media": bool(media_url)}


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


def check_browser_session() -> None:
	request = getattr(frappe.local, "request", None)
	if request is None or request.method != "POST" or frappe.get_request_header("Authorization"):
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
	check_chat(chat)
	text = (text or "").strip()
	if not text:
		frappe.throw(frappe._("The message is empty."))
	beeper_chat_id, account = frappe.db.get_value("Chat", chat, ["beeper_chat_id", "account"])
	client = sync.client_for(frappe.get_doc("Chat Account", account))
	beeper_call(client.send_text, beeper_chat_id, text)
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
	return {"messages": [serialize(r) for r in rows]}
