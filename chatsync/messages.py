import json
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_system_timezone

from chatsync import formatting

SKIPPED_TYPES = {"REACTION", "NOTICE"}
KINDS = {"TEXT": "Text", "VOICE": "Voice", "IMAGE": "Image", "FILE": "File", "VIDEO": "File"}
TYPE_BY_KIND = {"Voice": "audio", "Image": "img"}
PUBLIC_KEYS = ("type", "mime_type", "file_name", "file_size")
MAX_BYTES = 50 * 1024 * 1024


def to_site_time(timestamp: str) -> datetime:
	"""Beeper sends UTC ISO strings; they are stored as naive datetimes in the site's system timezone."""
	moment = datetime.fromisoformat(timestamp)
	return moment.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def sort_order(sort_key: str | None) -> tuple[int, int | str]:
	if sort_key and sort_key.isdigit():
		return (0, int(sort_key))
	return (1, sort_key or "")


def from_beeper(raw: dict) -> dict:
	return {
		"type": raw.get("type") or "unknown",
		"mime_type": raw.get("mimeType"),
		"file_name": raw.get("fileName"),
		"file_size": raw.get("fileSize"),
		"src_url": raw.get("srcURL"),
	}


def attachments_of(row) -> list[dict]:
	"""Messages stored before attachments were kept only have media_url."""
	if row.get("attachments"):
		return json.loads(row["attachments"])
	if row.get("media_url"):
		return [{"type": TYPE_BY_KIND.get(row.get("kind"), "unknown"), "src_url": row["media_url"]}]
	return []


def too_large(item: dict) -> bool:
	return (item.get("file_size") or 0) > MAX_BYTES


def public(attachments: list[dict]) -> list[dict]:
	return [
		{**{key: item.get(key) for key in PUBLIC_KEYS}, "too_large": too_large(item)} for item in attachments
	]


def is_shown(raw: dict) -> bool:
	return raw.get("type") not in SKIPPED_TYPES and not raw.get("isHidden") and not raw.get("isDeleted")


def values_from(raw: dict) -> dict:
	attachments = raw.get("attachments") or []
	kind = KINDS.get(raw.get("type"), "Other")
	return {
		"sort_key": raw.get("sortKey"),
		"sent_at": to_site_time(raw["timestamp"]),
		"direction": "Out" if raw.get("isSender") else "In",
		"sender_name": raw.get("senderName"),
		"kind": kind,
		"beeper_type": raw.get("type") if kind == "Other" else None,
		"text": raw.get("text"),
		"media_url": attachments[0].get("srcURL") if attachments else None,
		"attachments": json.dumps([from_beeper(a) for a in attachments]) if attachments else None,
	}


def preview(raw: dict) -> dict:
	"""A Beeper message shaped like a stored one for the chat view, without its media URLs."""
	values = values_from(raw)
	attachments = [from_beeper(a) for a in raw.get("attachments") or []]
	return {
		"name": raw["id"],
		"sent_at": str(values["sent_at"]),
		"direction": values["direction"],
		"sender_name": values["sender_name"],
		"kind": values["kind"],
		"beeper_type": values["beeper_type"],
		"text": values["text"],
		"transcript": None,
		"html": formatting.render(values["text"]),
		"has_media": bool(attachments),
		"attachments": public(attachments),
	}


def upsert(chat: str, raw: dict) -> str | None:
	if raw.get("type") in SKIPPED_TYPES or raw.get("isHidden"):
		return None
	existing = frappe.db.get_value("Chat Message", {"chat": chat, "beeper_message_id": raw["id"]})
	if raw.get("isDeleted"):
		if existing:
			frappe.delete_doc("Chat Message", existing, ignore_permissions=True, force=True)
		return None
	values = values_from(raw)
	if not existing:
		try:
			doc = frappe.get_doc(
				{"doctype": "Chat Message", "chat": chat, "beeper_message_id": raw["id"], **values}
			)
			doc.insert(ignore_permissions=True)
			return doc.name
		except frappe.UniqueValidationError:
			existing = frappe.db.get_value("Chat Message", {"chat": chat, "beeper_message_id": raw["id"]})
	current = frappe.db.get_value("Chat Message", existing, list(values), as_dict=True)
	changed = {k: v for k, v in values.items() if (current[k] or None) != (v or None)}
	if changed:
		frappe.db.set_value("Chat Message", existing, changed)
	return existing
