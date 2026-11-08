from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from chatsync import sync
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
		frappe.db.delete(
			"Chat Identifier", {"parenttype": "Contact", "beeper_chat_id": beeper_chat_id, "excluded": 1}
		)
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
