import re

import frappe
from werkzeug.wrappers import Response

from chatsync import api, transcription
from chatsync.beeper import VOICE_FALLBACK, BeeperError

GONE = (404, 410)
RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def load_audio(message: str) -> tuple[bytes, str]:
	chat, kind, media_url = frappe.db.get_value("Chat Message", message, ["chat", "kind", "media_url"])
	api.check_chat(chat)
	if kind != "Voice" or not media_url:
		frappe.throw(frappe._("This message is not a voice message."))
	client, _beeper_chat_id = api.chat_client(chat)
	try:
		audio, content_type = client.asset(media_url)
	except BeeperError as e:
		if e.status in GONE:
			frappe.throw("Die Sprachnachricht ist in Beeper nicht mehr verfügbar.")
		frappe.throw(frappe._("The voice message could not be loaded from Beeper."))
	if not content_type.lower().startswith("audio/"):
		content_type = VOICE_FALLBACK
	return audio, content_type


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


@frappe.whitelist()
def play(message: str) -> Response:
	audio, content_type = load_audio(message)
	size = len(audio)
	response = Response(audio, mimetype=content_type)
	response.headers["Content-Disposition"] = 'inline; filename="voice.ogg"'
	response.headers["Cache-Control"] = "no-store"
	response.headers["X-Content-Type-Options"] = "nosniff"
	response.headers["Accept-Ranges"] = "bytes"
	if requested := byte_range(frappe.get_request_header("Range"), size):
		first, last = requested
		if first >= size:
			response = Response(status=416)
			response.headers["Content-Range"] = f"bytes */{size}"
			response.headers["Accept-Ranges"] = "bytes"
			response.headers["Cache-Control"] = "no-store"
			return response
		response.status_code = 206
		response.data = audio[first : last + 1]
		response.headers["Content-Range"] = f"bytes {first}-{last}/{size}"
	return response


@frappe.whitelist(methods=["POST"])
def transcribe(message: str) -> str:
	service = transcription.provider()
	audio, content_type = load_audio(message)
	text = service.transcribe(audio, content_type)
	if not text:
		frappe.throw(frappe._("The transcription is empty."))
	frappe.db.set_value(
		"Chat Message",
		message,
		{"transcript": text, "transcript_provider": service.name},
		update_modified=False,
	)
	return text
