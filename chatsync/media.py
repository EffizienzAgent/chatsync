"""Attachments are streamed from Beeper on request and never stored."""

import re
from urllib.parse import quote

import frappe
from werkzeug.wrappers import Response

from chatsync import api
from chatsync.beeper import VOICE_FALLBACK, BeeperError
from chatsync.messages import attachments_of

GONE = (404, 410)
RANGE = re.compile(r"bytes=(\d*)-(\d*)")
DOWNLOAD = "application/octet-stream"
VIDEO_TYPES = {"video/mp4", "video/webm"}
IMAGE_SIGNATURES = (
	(b"\xff\xd8\xff", "image/jpeg"),
	(b"\x89PNG\r\n\x1a\n", "image/png"),
	(b"GIF87a", "image/gif"),
	(b"GIF89a", "image/gif"),
)


def sniff_image(data: bytes) -> str | None:
	for signature, content_type in IMAGE_SIGNATURES:
		if data.startswith(signature):
			return content_type
	if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
		return "image/webp"
	return None


def fetch(chat: str, src_url: str) -> tuple[bytes, str]:
	client, _beeper_chat_id = api.chat_client(chat)
	try:
		return client.asset(src_url)
	except BeeperError as e:
		if e.status in GONE:
			frappe.throw(frappe._("This file is no longer available in Beeper."))
		frappe.throw(frappe._("The file could not be loaded from Beeper."))


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
	match = RANGE.fullmatch((header or "").strip())
	if not match:
		return None
	start, end = match.groups()
	if not start:
		if not end or int(end) == 0:
			return (size, size - 1)
		return (max(size - int(end), 0), size - 1)
	first = int(start)
	last = min(int(end), size - 1) if end else size - 1
	return (first, last) if last >= first else (size, size - 1)


def safe_file_name(name: str | None) -> str:
	return re.sub(r"[^A-Za-z0-9._ -]", "_", name or "") or "file"


def disposition(kind: str, name: str | None) -> str:
	header = f'{kind}; filename="{safe_file_name(name)}"'
	if name:
		header += f"; filename*=UTF-8''{quote(name, safe='')}"
	return header


def respond(
	data: bytes, content_type: str, content_disposition: str, cache: str = "no-store", ranges: bool = False
) -> Response:
	response = Response(data, mimetype=content_type)
	response.headers["Content-Disposition"] = content_disposition
	response.headers["Cache-Control"] = cache
	response.headers["X-Content-Type-Options"] = "nosniff"
	if not ranges:
		return response
	size = len(data)
	response.headers["Accept-Ranges"] = "bytes"
	if requested := byte_range(frappe.get_request_header("Range"), size):
		first, last = requested
		if first >= size:
			response = Response(status=416)
			response.headers["Content-Range"] = f"bytes */{size}"
			response.headers["Accept-Ranges"] = "bytes"
			response.headers["Cache-Control"] = "no-store"
			response.headers["X-Content-Type-Options"] = "nosniff"
			return response
		response.status_code = 206
		response.data = data[first : last + 1]
		response.headers["Content-Range"] = f"bytes {first}-{last}/{size}"
	return response


def media_response(item: dict, data: bytes, upstream: str) -> Response:
	declared = [t.lower() for t in (item.get("mime_type"), upstream) if t]
	kind = item.get("type")
	inline = disposition("inline", item.get("file_name"))
	if kind == "img" or any(t.startswith("image/") for t in declared):
		if image := sniff_image(data):
			return respond(data, image, inline, "private, max-age=300")
	elif kind == "audio" or any(t.startswith("audio/") for t in declared):
		audio = next((t for t in declared if t.startswith("audio/")), VOICE_FALLBACK)
		return respond(data, audio, inline, ranges=True)
	elif kind == "video" and (video := next((t for t in declared if t in VIDEO_TYPES), None)):
		return respond(data, video, inline, ranges=True)
	return respond(data, DOWNLOAD, disposition("attachment", item.get("file_name")))


@frappe.whitelist(methods=["GET"])
def serve(message: str, index: int = 0) -> Response:
	row = frappe.db.get_value(
		"Chat Message", message, ["chat", "kind", "media_url", "attachments"], as_dict=True
	)
	if not row:
		frappe.throw(frappe._("Chat Message {0} not found").format(message), frappe.DoesNotExistError)
	api.check_chat(row.chat)
	attachments = attachments_of(row)
	index = int(index)
	if not 0 <= index < len(attachments) or not attachments[index].get("src_url"):
		frappe.throw(frappe._("This message has no such attachment."))
	item = attachments[index]
	data, upstream = fetch(row.chat, item["src_url"])
	return media_response(item, data, upstream)
