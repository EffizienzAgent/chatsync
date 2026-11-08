from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_system_timezone

SKIPPED_TYPES = {"REACTION", "NOTICE"}
KINDS = {"TEXT": "Text", "VOICE": "Voice", "IMAGE": "Image", "FILE": "File", "VIDEO": "File"}


def to_site_time(timestamp: str) -> datetime:
	"""Beeper sends UTC ISO strings; they are stored as naive datetimes in the site's system timezone."""
	moment = datetime.fromisoformat(timestamp)
	return moment.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def sort_order(sort_key: str | None) -> tuple[int, int | str]:
	if sort_key and sort_key.isdigit():
		return (0, int(sort_key))
	return (1, sort_key or "")


def upsert(chat: str, raw: dict) -> str | None:
	if raw.get("type") in SKIPPED_TYPES or raw.get("isHidden"):
		return None
	existing = frappe.db.get_value("Chat Message", {"chat": chat, "beeper_message_id": raw["id"]})
	if raw.get("isDeleted"):
		if existing:
			frappe.delete_doc("Chat Message", existing, ignore_permissions=True, force=True)
		return None
	attachments = raw.get("attachments") or []
	values = {
		"sort_key": raw.get("sortKey"),
		"sent_at": to_site_time(raw["timestamp"]),
		"direction": "Out" if raw.get("isSender") else "In",
		"sender_name": raw.get("senderName"),
		"kind": KINDS.get(raw.get("type"), "Other"),
		"text": raw.get("text"),
		"media_url": attachments[0].get("srcURL") if attachments else None,
	}
	if existing:
		doc = frappe.get_doc("Chat Message", existing)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc.name
	doc = frappe.get_doc({"doctype": "Chat Message", "chat": chat, "beeper_message_id": raw["id"], **values})
	doc.insert(ignore_permissions=True)
	return doc.name
