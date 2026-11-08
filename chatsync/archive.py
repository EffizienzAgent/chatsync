import base64
from datetime import datetime, time

import frappe
from frappe import _
from frappe.utils import add_days, escape_html, format_datetime, getdate
from frappe.utils.pdf import get_pdf

from chatsync.api import check_chat
from chatsync.formatting import render

DOCUMENT_TYPE = "correspondence"
SOURCE_SYSTEM = "chatsync"
STYLE = "body{font-family:sans-serif;font-size:11px}td,th{padding:3px 6px;vertical-align:top;text-align:left}"


def render_pdf(html: str) -> bytes:
	return get_pdf(html)


def check_archive_role() -> None:
	from docarchive.api import ALLOWED_ROLES

	if not set(ALLOWED_ROLES) & set(frappe.get_roles()):
		frappe.throw(
			_("Archiving needs one of the roles {0}").format(", ".join(ALLOWED_ROLES)), frappe.PermissionError
		)


def messages_between(chat: str, start, end) -> list[dict]:
	filters = [["chat", "=", chat]]
	if start:
		filters.append(["sent_at", ">=", datetime.combine(start, time.min)])
	if end:
		filters.append(["sent_at", "<", datetime.combine(add_days(end, 1), time.min)])
	return frappe.get_all(
		"Chat Message",
		filters=filters,
		fields=["sent_at", "direction", "sender_name", "kind", "text", "transcript", "media_url"],
		order_by="sent_at asc, name asc",
	)


def content_of(row: dict) -> str:
	parts = [render(row.text)] if row.text else []
	if row.transcript:
		parts.append("🎤 " + escape_html(row.transcript))
	if not parts and (row.media_url or row.kind not in ("Text", "Voice")):
		parts.append("📎 " + escape_html(_("File")))
	return "<br>".join(parts)


def transcript_html(chat, rows: list[dict], start, end) -> str:
	period = f"{start} - {end}"
	lines = "".join(
		"<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
			escape_html(format_datetime(row.sent_at, "yyyy-MM-dd HH:mm")),
			escape_html(row.sender_name or (_("Me") if row.direction == "Out" else "")),
			content_of(row),
		)
		for row in rows
	)
	return (
		f"<html><head><meta charset='utf-8'><style>{STYLE}</style></head><body>"
		f"<h3>{escape_html(chat.title or chat.name)}</h3>"
		f"<p>{escape_html(_('Contact'))}: {escape_html(chat.contact)}<br>"
		f"{escape_html(_('Network'))}: {escape_html(chat.network or '')}<br>"
		f"{escape_html(_('Period'))}: {escape_html(period)}</p>"
		f"<table>{lines}</table></body></html>"
	)


@frappe.whitelist(methods=["POST"])
def archive_chat(chat: str, date_from: str | None = None, date_to: str | None = None) -> str:
	check_chat(chat, write=True)
	check_archive_role()
	from docarchive.api import upload_document

	requested_from, requested_to = (getdate(d) if d else None for d in (date_from, date_to))
	rows = messages_between(chat, requested_from, requested_to)
	if not rows:
		frappe.throw(_("There are no messages in this period"))
	start = requested_from or getdate(rows[0].sent_at)
	end = requested_to or getdate(rows[-1].sent_at)
	doc = frappe.get_doc("Chat", chat)
	contact_name = frappe.db.get_value("Contact", doc.contact, "first_name") or doc.contact
	data = render_pdf(transcript_html(doc, rows, start, end))
	result = upload_document(
		document_type=DOCUMENT_TYPE,
		document_date=str(end),
		file_name=f"chat-{doc.network}-{contact_name}-{start}-{end}.pdf".replace("/", "-").replace(" ", "-"),
		content_base64=base64.b64encode(data).decode(),
		title=_("Chat with {0}").format(contact_name),
		source_system=SOURCE_SYSTEM,
		source_account=doc.account,
		source_party_type="Contact",
		source_party_id=doc.contact,
		source_party_name=contact_name,
		source_key=f"chat:{chat}:{date_from or ''}:{date_to or ''}",
	)
	return result["name"]


def assign(document) -> None:
	if document.source_system != SOURCE_SYSTEM or document.counterparty:
		return
	if document.source_party_id and frappe.db.exists("Contact", document.source_party_id):
		document.counterparty_type, document.counterparty = "Contact", document.source_party_id
