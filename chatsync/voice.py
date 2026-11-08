import frappe
from werkzeug.wrappers import Response

from chatsync import api, media, transcription
from chatsync.beeper import VOICE_FALLBACK, AssetTooLarge, BeeperError


def load_audio(message: str, write: bool = False) -> tuple[bytes, str]:
	chat, kind, media_url = frappe.db.get_value("Chat Message", message, ["chat", "kind", "media_url"])
	api.check_chat(chat, write)
	if kind != "Voice" or not media_url:
		frappe.throw(frappe._("This message is not a voice message."))
	client, _beeper_chat_id = api.chat_client(chat)
	try:
		audio, content_type = client.asset(media_url, media.MAX_BYTES)
	except AssetTooLarge:
		media.refuse_too_large()
	except BeeperError as e:
		if e.status in media.GONE:
			frappe.throw(frappe._("This voice message is no longer available in Beeper."))
		frappe.throw(frappe._("The voice message could not be loaded from Beeper."))
	if not content_type.lower().startswith("audio/"):
		content_type = VOICE_FALLBACK
	return audio, content_type


@frappe.whitelist()
def play(message: str) -> Response:
	audio, content_type = load_audio(message)
	return media.respond(audio, content_type, 'inline; filename="voice.ogg"', ranges=True)


@frappe.whitelist(methods=["POST"])
def transcribe(message: str) -> str:
	service = transcription.provider()
	audio, content_type = load_audio(message, write=True)
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
