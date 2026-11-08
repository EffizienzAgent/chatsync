from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from chatsync import api, sync, transcription, voice
from chatsync.beeper import BeeperError
from chatsync.messages import upsert
from chatsync.tests.helpers import tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json, message

SRC = "mxc://local.beeper.com/abc?encryptedFileInfoJSON=x"
FAKE_KEY = "test-key-not-real"


class VoiceBeeper(AssignmentBeeper):
	def __init__(self, *args):
		super().__init__(*args)
		self.audio = (b"OggS-bytes", "audio/ogg")
		self.error = None
		self.requested = []

	def asset(self, url):
		self.requested.append(url)
		if self.error:
			raise self.error
		return self.audio


class TestVoice(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("Chat Account", {"user": USER})
		frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		raw = chat_json(number)
		self.fake = VoiceBeeper([raw], {})
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.chat = api.assign_chat(raw["id"], make_contact(phones=[number]))
		frappe.db.delete("Chat Message", {"chat": self.chat})
		self.voice = frappe.get_doc(
			"Chat Message",
			upsert(
				self.chat,
				message(raw["id"], 0, None, type="VOICE", attachments=[{"srcURL": SRC}]),
			),
		)
		settings = frappe.get_single("Chat Sync Settings")
		settings.mistral_api_key = FAKE_KEY
		settings.save(ignore_permissions=True)

	def response(self):
		return frappe.local.response

	def test_play_streams_without_storing(self):
		files = frappe.db.count("File")
		voice.play(self.voice.name)
		response = self.response()
		self.assertEqual(response["filecontent"], b"OggS-bytes")
		self.assertEqual(response["type"], "download")
		self.assertEqual(response["content_type"], "audio/ogg")
		self.assertEqual(response["display_content_as"], "inline")
		self.assertEqual(frappe.local.response_headers["Cache-Control"], "no-store")
		self.assertEqual(self.fake.requested, [SRC])
		self.assertEqual(frappe.db.count("File"), files)

	def test_play_voice_with_octet_stream_is_served_as_ogg(self):
		self.fake.audio = (b"x", "application/octet-stream")
		voice.play(self.voice.name)
		self.assertEqual(self.response()["content_type"], "audio/ogg")

	def test_transcribe_stores_text(self):
		reply = MagicMock(ok=True)
		reply.json.return_value = {"text": "Hallo Welt"}
		with patch("chatsync.transcription.requests.post", return_value=reply) as post:
			result = voice.transcribe(self.voice.name)
		self.assertEqual(result, "Hallo Welt")
		args, kwargs = post.call_args
		self.assertEqual(args[0], "https://api.mistral.ai/v1/audio/transcriptions")
		self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {FAKE_KEY}"})
		self.assertEqual(kwargs["data"], {"model": "voxtral-mini-transcribe-3"})
		self.assertEqual(kwargs["files"]["file"], ("voice.ogg", b"OggS-bytes", "audio/ogg"))
		self.voice.reload()
		self.assertEqual(self.voice.transcript, "Hallo Welt")
		self.assertEqual(self.voice.transcript_provider, "Mistral")

	def test_transcribe_failure_does_not_leak_key(self):
		reply = MagicMock(ok=False, status_code=401)
		with patch("chatsync.transcription.requests.post", return_value=reply):
			with self.assertRaises(frappe.ValidationError) as ctx:
				voice.transcribe(self.voice.name)
		self.assertNotIn(FAKE_KEY, str(ctx.exception))
		self.voice.reload()
		self.assertFalse(self.voice.transcript)

	def test_transcribe_without_key(self):
		frappe.local.lang = "de"
		self.addCleanup(setattr, frappe.local, "lang", "en")
		settings = frappe.get_single("Chat Sync Settings")
		settings.mistral_api_key = None
		settings.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			voice.transcribe(self.voice.name)
		self.assertIn("Kein Transkriptionsdienst konfiguriert", str(ctx.exception))
		self.assertEqual(self.fake.requested, [])

	def test_voice_owner_only(self):
		other = make_user("other", ["Chat User"])
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			voice.play(self.voice.name)
		with self.assertRaises(frappe.PermissionError):
			voice.transcribe(self.voice.name)
		self.assertEqual(self.fake.requested, [])

	def test_missing_audio_message(self):
		for status in (404, 410):
			self.fake.error = BeeperError(status, "gone")
			with self.assertRaises(frappe.ValidationError) as ctx:
				voice.play(self.voice.name)
			self.assertIn("Die Sprachnachricht ist in Beeper nicht mehr verfügbar.", str(ctx.exception))

	def test_other_beeper_error_is_generic(self):
		self.fake.error = BeeperError(500, "boom")
		with self.assertRaises(frappe.ValidationError) as ctx:
			voice.play(self.voice.name)
		self.assertNotIn("nicht mehr verfügbar", str(ctx.exception))

	def test_non_voice_message_rejected(self):
		self.voice.db_set("kind", "Image")
		with self.assertRaises(frappe.ValidationError):
			voice.play(self.voice.name)

	def test_provider_factory(self):
		provider = transcription.provider()
		self.assertIsInstance(provider, transcription.MistralProvider)
