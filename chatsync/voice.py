import frappe

from chatsync import api, transcription
from chatsync.beeper import VOICE_FALLBACK, BeeperError

GONE = (404, 410)


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
	if content_type.split(";")[0].strip() == "application/octet-stream":
		content_type = VOICE_FALLBACK
	return audio, content_type


@frappe.whitelist()
def play(message: str) -> None:
	audio, content_type = load_audio(message)
	response = frappe.local.response
	response.type = "download"
	response.filename = "voice.ogg"
	response.filecontent = audio
	response.content_type = content_type
	response.display_content_as = "inline"
	frappe.local.response_headers["Cache-Control"] = "no-store"


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
