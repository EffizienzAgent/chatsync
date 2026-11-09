"""Attachments are streamed from Beeper on request and never stored."""

import re
from urllib.parse import quote

import frappe
from werkzeug.wrappers import Response

from chatsync import api, sync
from chatsync.beeper import VOICE_FALLBACK, AssetTooLarge, BeeperError
from chatsync.messages import MAX_BYTES, attachments_of, too_large

GONE = (404, 410)
UNSATISFIABLE = 416
RANGE = re.compile(r"bytes=(\d*)-(\d*)")
DOWNLOAD = "application/octet-stream"
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_CACHE = "private, max-age=86400"
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


class FileTooLarge(frappe.ValidationError):
	http_status_code = 413


def refuse_too_large():
	frappe.throw(frappe._("File too large for the preview, please open it in Beeper"), FileTooLarge)


def fetch(function, *args):
	try:
		return function(*args)
	except AssetTooLarge:
		refuse_too_large()
	except BeeperError as e:
		if e.status == UNSATISFIABLE:
			raise
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


def declared_types(item: dict, upstream: str) -> list[str]:
	return [t.lower() for t in (item.get("mime_type"), upstream) if t]


def is_image(item: dict, upstream: str = "") -> bool:
	return item.get("type") == "img" or any(t.startswith("image/") for t in declared_types(item, upstream))


def stream_type(item: dict, upstream: str = "") -> str | None:
	"""Content type for audio and MP4/WebM video, which play inline with Range support."""
	declared = declared_types(item, upstream)
	if is_image(item, upstream):
		return None
	if item.get("type") == "audio" or any(t.startswith("audio/") for t in declared):
		return next((t for t in declared if t.startswith("audio/")), VOICE_FALLBACK)
	if item.get("type") == "video":
		return next((t for t in declared if t in VIDEO_TYPES), None)
	return None


def media_response(item: dict, data: bytes, upstream: str) -> Response:
	inline = disposition("inline", item.get("file_name"))
	if is_image(item, upstream):
		if image := sniff_image(data):
			return respond(data, image, inline, "private, max-age=300")
	elif content_type := stream_type(item, upstream):
		return respond(data, content_type, inline, ranges=True)
	return respond(data, DOWNLOAD, disposition("attachment", item.get("file_name")))


def ranged_response(client, item: dict, range_header: str) -> Response:
	try:
		data, upstream, status, content_range = fetch(
			client.asset_part, item["src_url"], range_header, MAX_BYTES
		)
	except BeeperError:
		response = Response(status=UNSATISFIABLE)
		response.headers["Content-Range"] = f"bytes */{item.get('file_size') or '*'}"
		response.headers["Cache-Control"] = "no-store"
		response.headers["X-Content-Type-Options"] = "nosniff"
		return response
	content_type = stream_type(item, upstream)
	inline = disposition("inline", item.get("file_name"))
	if status != 206 or not content_range:
		return respond(data, content_type, inline, ranges=True)
	response = respond(data, content_type, inline)
	response.status_code = 206
	response.headers["Content-Range"] = content_range
	response.headers["Accept-Ranges"] = "bytes"
	return response


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
	if too_large(item):
		refuse_too_large()
	client, _beeper_chat_id = api.chat_client(row.chat)
	range_header = frappe.get_request_header("Range")
	if range_header and RANGE.fullmatch(range_header.strip()) and stream_type(item):
		return ranged_response(client, item, range_header)
	data, upstream = fetch(client.asset, item["src_url"], MAX_BYTES)
	return media_response(item, data, upstream)


@frappe.whitelist(methods=["GET"])
def avatar(chat: str | None = None, beeper_chat_id: str | None = None) -> Response:
	"""The Beeper picture of the person in one of the caller's chats, streamed and never stored.

	Without a usable picture the answer is an empty 204, cached like a picture, so the browser
	shows the initials and does not ask again for a day.
	"""
	if chat:
		api.check_chat(chat, write=True)
		client, beeper_chat_id = api.chat_client(chat)
	elif beeper_chat_id:
		client = sync.client_for(api.session_account())
	else:
		frappe.throw(frappe._("No chat given."))
	source = api.avatar_source(fetch(client.chat, beeper_chat_id))
	data = fetch(client.asset, source, AVATAR_MAX_BYTES)[0] if source else b""
	if content_type := sniff_image(data):
		return respond(data, content_type, disposition("inline", "avatar"), AVATAR_CACHE)
	response = Response(status=204)
	response.headers["Cache-Control"] = AVATAR_CACHE
	response.headers["X-Content-Type-Options"] = "nosniff"
	return response
