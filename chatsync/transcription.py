import frappe
import requests

MISTRAL_URL = "https://api.mistral.ai/v1/audio/transcriptions"
TIMEOUT = 120


class MistralProvider:
	name = "Mistral"

	def __init__(self, api_key: str, model: str):
		self.api_key = api_key
		self.model = model

	def transcribe(self, audio: bytes, mime: str) -> str:
		try:
			response = requests.post(
				MISTRAL_URL,
				headers={"Authorization": f"Bearer {self.api_key}"},
				data={"model": self.model},
				files={"file": ("voice.ogg", audio, mime)},
				timeout=TIMEOUT,
			)
		except requests.RequestException as e:
			frappe.throw(frappe._("Transcription failed ({0})").format(type(e).__name__))
		if not response.ok:
			frappe.throw(frappe._("Transcription failed ({0})").format(response.status_code))
		return (response.json().get("text") or "").strip()


def provider() -> MistralProvider:
	settings = frappe.get_single("Chat Sync Settings")
	api_key = settings.get_password("mistral_api_key", raise_exception=False)
	if not api_key:
		frappe.throw(frappe._("No transcription service configured"))
	return MistralProvider(api_key, settings.mistral_model or "voxtral-mini-transcribe-3")
