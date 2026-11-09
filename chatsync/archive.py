import base64
from datetime import datetime, time

import frappe
from frappe import _
from frappe.utils import add_days, escape_html, format_datetime, getdate
from frappe.utils.pdf import get_pdf

from chatsync.api import check_chat
from chatsync.formatting import render
from chatsync.messages import attachments_of

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
		fields=[
			"sent_at",
			"direction",
			"sender_name",
			"kind",
			"text",
			"transcript",
			"media_url",
			"attachments",
		],
		order_by="sent_at asc, name asc",
	)


def attachment_line(item: dict) -> str:
	name = item.get("file_name")
	if item.get("type") == "img":
		return escape_html(_("[Image: {0}]").format(name) if name else _("[Image]"))
	return "📎 " + escape_html(name or _("File"))


def content_of(row: dict) -> str:
	parts = [render(row.text)] if row.text else []
	if row.kind != "Voice":
		parts.extend(attachment_line(item) for item in attachments_of(row))
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
		f"<p>{escape_html(_('Group') if chat.is_group else _('Contact'))}: "
		f"{escape_html(', '.join(p.party for p in chat.parties) if chat.is_group else chat.contact)}<br>"
		f"{escape_html(_('Network'))}: {escape_html(chat.network or '')}<br>"
		f"{escape_html(_('Period'))}: {escape_html(period)}</p>"
		f"<table>{lines}</table></body></html>"
	)


def archive_party(doc) -> tuple:
	"""A direct chat is filed under its contact, a group chat under its first party."""
	if doc.is_group:
		first = doc.parties[0] if doc.parties else None
		title = doc.title or doc.name
		return (first and first.party_type, first and first.party, title, _("Group chat {0}").format(title))
	name = frappe.db.get_value("Contact", doc.contact, "first_name") or doc.contact
	return "Contact", doc.contact, name, _("Chat with {0}").format(name)


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
	party_type, party, party_name, title = archive_party(doc)
	data = render_pdf(transcript_html(doc, rows, start, end))
	result = upload_document(
		document_type=DOCUMENT_TYPE,
		document_date=str(end),
		file_name=f"chat-{doc.network}-{party_name}-{start}-{end}.pdf".replace("/", "-").replace(" ", "-"),
		content_base64=base64.b64encode(data).decode(),
		title=title,
		source_system=SOURCE_SYSTEM,
		source_account=doc.account,
		source_party_type=party_type,
		source_party_id=party,
		source_party_name=party_name,
		source_key=f"chat:{chat}:{date_from or ''}:{date_to or ''}",
	)
	return result["name"]


def assign(document) -> None:
	if document.source_system != SOURCE_SYSTEM or document.counterparty:
		return
	from docarchive.counterparty import doctypes

	party_type, party = document.source_party_type, document.source_party_id
	allowed = doctypes()
	if allowed and party_type not in allowed:
		return
	if party_type and party and frappe.db.exists(party_type, party):
		document.counterparty_type, document.counterparty = party_type, party
