from urllib.parse import urlparse

import frappe
import requests

MISTRAL_URL = "https://api.mistral.ai/v1/audio/transcriptions"
OPENAI_COMPATIBLE = "OpenAI-compatible"
DEFAULT_MODEL = "whisper-1"
TIMEOUT = 120


def post_audio(url: str, api_key: str | None, data: dict, audio: bytes, mime: str) -> str:
	"""POSTs the audio as multipart `file` and returns the `text` of the reply; errors never contain the key."""
	headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
	try:
		response = requests.post(
			url, headers=headers, data=data, files={"file": ("voice.ogg", audio, mime)}, timeout=TIMEOUT
		)
	except requests.RequestException as e:
		frappe.throw(frappe._("Transcription failed ({0})").format(type(e).__name__))
	if not response.ok:
		frappe.throw(frappe._("Transcription failed ({0})").format(response.status_code))
	return (response.json().get("text") or "").strip()


class MistralProvider:
	name = "Mistral"

	def __init__(self, api_key: str, model: str):
		self.api_key = api_key
		self.model = model

	def transcribe(self, audio: bytes, mime: str) -> str:
		return post_audio(MISTRAL_URL, self.api_key, {"model": self.model}, audio, mime)


class OpenAICompatibleProvider:
	"""Any service with OpenAI's `POST {base}/audio/transcriptions`: self-hosted Whisper servers
	(speaches/faster-whisper-server, whisper.cpp server, LocalAI) or hosted APIs (OpenAI, Groq, ...)."""

	name = OPENAI_COMPATIBLE

	def __init__(self, base_url: str, api_key: str | None, model: str, language: str | None = None):
		self.url = base_url.rstrip("/") + "/audio/transcriptions"
		self.api_key = api_key
		self.model = model
		self.language = language

	def transcribe(self, audio: bytes, mime: str) -> str:
		data = {"model": self.model, **({"language": self.language} if self.language else {})}
		return post_audio(self.url, self.api_key, data, audio, mime)


def provider() -> MistralProvider | OpenAICompatibleProvider:
	settings = frappe.get_single("Chat Sync Settings")
	if settings.transcription_provider == OPENAI_COMPATIBLE:
		base_url = (settings.transcription_url or "").strip()
		if urlparse(base_url).scheme not in ("http", "https"):
			frappe.throw(frappe._("No transcription service configured"))
		return OpenAICompatibleProvider(
			base_url,
			settings.get_password("transcription_api_key", raise_exception=False) or None,
			(settings.transcription_model or "").strip() or DEFAULT_MODEL,
			(settings.transcription_language or "").strip() or None,
		)
	api_key = settings.get_password("mistral_api_key", raise_exception=False)
	if not api_key:
		frappe.throw(frappe._("No transcription service configured"))
	return MistralProvider(api_key, settings.mistral_model or "voxtral-mini-transcribe-3")
