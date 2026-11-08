import json
import os
from unittest.mock import patch

import frappe

from chatsync import api, media, sync
from chatsync.beeper import BeeperError
from chatsync.messages import upsert
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json, message
from chatsync.tests.test_voice import VoiceBeeper

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 20
GIF = b"GIF89a" + b"0" * 20
WEBP = b"RIFF0000WEBPVP8 " + b"0" * 20
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'
PDF = b"%PDF-1.7 ..."


def attachment(src, type="img", mime=None, name=None, size=None):
	values = {"type": type, "srcURL": src}
	if mime:
		values["mimeType"] = mime
	if name:
		values["fileName"] = name
	if size:
		values["fileSize"] = size
	return values


class AssetBeeper(VoiceBeeper):
	def __init__(self, *args):
		super().__init__(*args)
		self.assets = {}

	def asset(self, url):
		self.requested.append(url)
		if self.error:
			raise self.error
		return self.assets[url]


def files_on_disk():
	return sum(len(files) for _root, _dirs, files in os.walk(frappe.get_site_path()))


class TestMedia(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("Chat Account", {"user": USER})
		frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		self.raw = chat_json(number)
		self.fake = AssetBeeper([self.raw], {})
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.chat = api.assign_chat(self.raw["id"], make_contact(phones=[number]))
		frappe.db.delete("Chat Message", {"chat": self.chat})

	def store(self, type, *attachments, text=None):
		return upsert(self.chat, message(self.raw["id"], 0, text, type=type, attachments=list(attachments)))

	def image(self, data, mime="image/png", type="img", name="photo.png"):
		self.fake.assets["mxc://img"] = (data, "application/octet-stream")
		return self.store("IMAGE", attachment("mxc://img", type, mime, name, len(data)))

	def serve(self, name, index=0, range_header=None):
		with patch.object(
			frappe, "get_request_header", lambda k, d=None: range_header if k == "Range" else d
		):
			return media.serve(name, index)

	def assert_download(self, response, file_name):
		self.assertEqual(response.mimetype, "application/octet-stream")
		self.assertEqual(response.headers["Cache-Control"], "no-store")
		self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
		disposition = response.headers["Content-Disposition"]
		self.assertTrue(disposition.startswith("attachment;"), disposition)
		self.assertIn(file_name, disposition)

	def test_upsert_stores_all_attachments(self):
		name = self.store(
			"IMAGE",
			attachment("mxc://a", "img", "image/jpeg", "a.jpg", 1200),
			attachment("mxc://b", "img", "image/png", "b.png", 3400),
			text="Zwei Bilder",
		)
		row = frappe.db.get_value("Chat Message", name, ["media_url", "attachments", "kind"], as_dict=True)
		self.assertEqual(row.media_url, "mxc://a")
		self.assertEqual(row.kind, "Image")
		self.assertEqual(
			json.loads(row.attachments),
			[
				{
					"type": "img",
					"mime_type": "image/jpeg",
					"file_name": "a.jpg",
					"file_size": 1200,
					"src_url": "mxc://a",
				},
				{
					"type": "img",
					"mime_type": "image/png",
					"file_name": "b.png",
					"file_size": 3400,
					"src_url": "mxc://b",
				},
			],
		)

	def test_text_message_has_no_attachments(self):
		name = self.store("TEXT", text="hi")
		self.assertIsNone(frappe.db.get_value("Chat Message", name, "attachments"))

	def test_get_messages_lists_attachments_without_source(self):
		self.store("FILE", attachment("mxc://f", "unknown", "application/pdf", "Angebot.pdf", 2048))
		[row] = api.get_messages(self.chat)["messages"]
		self.assertEqual(
			row["attachments"],
			[
				{
					"type": "unknown",
					"mime_type": "application/pdf",
					"file_name": "Angebot.pdf",
					"file_size": 2048,
				}
			],
		)
		self.assertNotIn("mxc://f", json.dumps(row))

	def test_image_served_inline_without_storing(self):
		files, disk = frappe.db.count("File"), files_on_disk()
		for data, expected in (
			(PNG, "image/png"),
			(JPEG, "image/jpeg"),
			(GIF, "image/gif"),
			(WEBP, "image/webp"),
		):
			with self.subTest(expected):
				response = self.serve(self.image(data))
				self.assertEqual(response.status_code, 200)
				self.assertEqual(response.data, data)
				self.assertEqual(response.mimetype, expected)
				self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))
				self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")
				self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(frappe.db.count("File"), files)
		self.assertEqual(files_on_disk(), disk)

	def test_svg_is_never_inline(self):
		for mime in ("image/svg+xml", "image/png"):
			with self.subTest(mime):
				self.assert_download(self.serve(self.image(SVG, mime=mime, name="x.svg")), "x.svg")

	def test_image_with_foreign_bytes_is_a_download(self):
		self.assert_download(self.serve(self.image(b"<html><script>", name="fake.png")), "fake.png")

	def test_file_is_a_download(self):
		self.fake.assets["mxc://f"] = (PDF, "application/pdf")
		name = self.store("FILE", attachment("mxc://f", "unknown", "application/pdf", 'An"gebot\r\n.pdf', 12))
		response = self.serve(name)
		self.assertEqual(response.data, PDF)
		self.assert_download(response, "An_gebot__.pdf")
		self.assertNotIn("\n", response.headers["Content-Disposition"])

	def test_index_selects_attachment(self):
		self.fake.assets["mxc://a"] = (PNG, "")
		self.fake.assets["mxc://b"] = (PDF, "")
		name = self.store("IMAGE", attachment("mxc://a"), attachment("mxc://b", "unknown", name="b.pdf"))
		self.assertEqual(self.serve(name, 1).data, PDF)
		self.assertEqual(self.fake.requested, ["mxc://b"])
		with self.assertRaises(frappe.ValidationError):
			self.serve(name, 2)

	def test_video_inline_with_range(self):
		self.fake.assets["mxc://v"] = (b"0123456789", "")
		name = self.store("VIDEO", attachment("mxc://v", "video", "video/mp4", "clip.mp4"))
		response = self.serve(name, range_header="bytes=2-4")
		self.assertEqual(response.status_code, 206)
		self.assertEqual(response.data, b"234")
		self.assertEqual(response.mimetype, "video/mp4")
		self.assertEqual(response.headers["Content-Range"], "bytes 2-4/10")
		self.assertEqual(response.headers["Cache-Control"], "no-store")

	def test_other_video_is_a_download(self):
		self.fake.assets["mxc://v"] = (b"0123", "video/quicktime")
		name = self.store("VIDEO", attachment("mxc://v", "video", "video/quicktime", "clip.mov"))
		self.assert_download(self.serve(name), "clip.mov")

	def test_audio_keeps_audio_type(self):
		self.fake.assets["mxc://v"] = (b"OggS", "")
		name = self.store("VOICE", attachment("mxc://v", "audio", "audio/ogg"))
		response = self.serve(name)
		self.assertEqual(response.mimetype, "audio/ogg")
		self.assertEqual(response.headers["Accept-Ranges"], "bytes")

	def test_message_from_before_attachments_were_stored(self):
		name = self.image(PNG)
		frappe.db.set_value("Chat Message", name, "attachments", None)
		self.assertEqual(self.serve(name).mimetype, "image/png")

	def test_owner_only(self):
		name = self.image(PNG)
		frappe.set_user(make_user("other", ["Chat User"]))
		with self.assertRaises(frappe.PermissionError):
			self.serve(name)
		self.assertEqual(self.fake.requested, [])

	def test_message_without_media(self):
		with self.assertRaises(frappe.ValidationError):
			self.serve(self.store("TEXT", text="hi"))

	def test_gone_asset(self):
		name = self.image(PNG)
		self.fake.error = BeeperError(404, "gone")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.serve(name)
		self.assertIn("no longer available", str(ctx.exception))
