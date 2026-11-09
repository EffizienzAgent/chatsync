from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlencode

import frappe

from chatsync import api, media, sync
from chatsync.beeper import AssetTooLarge
from chatsync.tests.helpers import ChatsyncTestCase
from chatsync.tests.test_inbox_unassigned import PagedBeeper, number
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_media import GIF, JPEG, PDF, PNG, SVG, WEBP, AssetBeeper, files_on_disk
from chatsync.tests.test_merged_view import add, make_account
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import chat_json

FACE = "mxc://beeper.local/face"
ENDPOINT = "/api/method/chatsync.media.avatar?"


def with_picture(raw, url=FACE):
	raw["participants"]["items"][1]["imgURL"] = url
	return raw


class AvatarCase(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		self.user = make_user("avatar", ["Chat User", "System Manager"])
		self.account = make_account(self.user)
		self.raw = with_picture(chat_json(number()))
		self.fake = AssetBeeper([self.raw], {})
		self.fake.assets[FACE] = (PNG, "application/octet-stream")
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.contact = make_contact()
		self.chat = (
			frappe.get_doc(
				{
					"doctype": "Chat",
					"account": self.account.name,
					"user": self.user,
					"beeper_chat_id": self.raw["id"],
					"network": "whatsapp",
					"title": "Other",
					"contact": self.contact,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.set_user(self.user)

	def endpoint(self, **args):
		return ENDPOINT + urlencode(args)


class TestAvatarEndpoint(AvatarCase):
	def assert_no_picture(self, response):
		self.assertEqual(response.status_code, 204)
		self.assertEqual(response.data, b"")
		self.assertEqual(response.headers["Cache-Control"], "private, max-age=86400")

	def test_streams_counterpart_picture_without_storing(self):
		files, disk = frappe.db.count("File"), files_on_disk()
		response = media.avatar(chat=self.chat)
		self.assertEqual((response.status_code, response.mimetype, response.data), (200, "image/png", PNG))
		self.assertEqual(response.headers["Cache-Control"], "private, max-age=86400")
		self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
		self.assertEqual(self.fake.requested, [FACE])
		self.assertEqual(self.fake.limits, [2 * 1024 * 1024])
		self.assertEqual((frappe.db.count("File"), files_on_disk()), (files, disk))

	def test_falls_back_to_chat_picture(self):
		del self.raw["participants"]["items"][1]["imgURL"]
		self.raw["imgURL"] = "file:///beeper/media/chat.jpg"
		self.fake.assets["file:///beeper/media/chat.jpg"] = (JPEG, "image/jpeg")
		self.assertEqual(media.avatar(chat=self.chat).mimetype, "image/jpeg")

	def test_only_sniffed_raster_images(self):
		for data, content_type in ((JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")):
			self.fake.assets[FACE] = (data, "image/svg+xml")
			self.assertEqual(media.avatar(chat=self.chat).mimetype, content_type)
		for data in (SVG, PDF):
			self.fake.assets[FACE] = (data, "image/png")
			self.assert_no_picture(media.avatar(chat=self.chat))

	def test_without_picture_nothing_is_fetched(self):
		del self.raw["participants"]["items"][1]["imgURL"]
		self.assert_no_picture(media.avatar(chat=self.chat))
		with_picture(self.raw, "https://example.com/face.png")
		self.assert_no_picture(media.avatar(chat=self.chat))
		self.assertEqual(self.fake.requested, [])

	def test_size_cap(self):
		self.fake.error = AssetTooLarge(media.AVATAR_MAX_BYTES)
		with self.assertRaises(media.FileTooLarge):
			media.avatar(chat=self.chat)

	def test_owner_only(self):
		for user in (make_user("avatar-other", ["Chat User"]), "Administrator"):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				media.avatar(chat=self.chat)
		self.assertEqual(self.fake.requested, [])

	def test_unassigned_chat_through_own_account(self):
		frappe.db.delete("Chat", {"name": self.chat})
		self.assertEqual(media.avatar(beeper_chat_id=self.raw["id"]).data, PNG)
		frappe.set_user(make_user("avatar-none", ["Chat User"]))
		with self.assertRaises(frappe.ValidationError):
			media.avatar(beeper_chat_id=self.raw["id"])
		self.assertEqual(self.fake.requested, [FACE])

	def test_needs_a_chat(self):
		with self.assertRaises(frappe.ValidationError):
			media.avatar()

	def test_avatar_is_get(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[media.avatar], ["GET"])


class TestAvatarUrls(AvatarCase):
	def test_contact_picture_comes_first(self):
		add(frappe._dict(name=self.chat), datetime(2026, 3, 1, 9))
		frappe.db.set_value("Chat", self.chat, "last_message_at", datetime(2026, 3, 1, 9))
		beeper = self.endpoint(chat=self.chat)
		self.assertEqual(api.get_contact_messages(self.contact)["avatar_url"], beeper)
		self.assertEqual(api.get_messages(self.chat)["chat"]["avatar_url"], beeper)
		self.assertEqual(api.get_conversations()[0]["avatar_url"], beeper)
		frappe.db.set_value("Contact", self.contact, "image", "/private/files/face.png")
		self.assertEqual(api.get_contact_messages(self.contact)["avatar_url"], "/private/files/face.png")
		self.assertEqual(api.get_messages(self.chat)["chat"]["avatar_url"], "/private/files/face.png")
		self.assertEqual(api.get_conversations()[0]["avatar_url"], "/private/files/face.png")

	def test_contact_without_chat_has_no_picture(self):
		self.assertIsNone(api.get_contact_messages(make_contact())["avatar_url"])

	def test_newest_chat_gives_the_beeper_picture(self):
		newer = (
			frappe.get_doc(
				{
					"doctype": "Chat",
					"account": self.account.name,
					"user": self.user,
					"beeper_chat_id": "!newer:beeper.local",
					"network": "signal",
					"contact": self.contact,
					"last_message_at": datetime(2026, 3, 2),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.set_value("Chat", self.chat, "last_message_at", datetime(2026, 3, 1))
		expected = self.endpoint(chat=newer)
		self.assertEqual(api.get_contact_messages(self.contact)["avatar_url"], expected)
		self.assertEqual(api.get_conversations()[0]["avatar_url"], expected)

	def test_unassigned_items_and_preview(self):
		frappe.db.delete("Chat", {"name": self.chat})
		plain = chat_json(number("16"))
		self.fake = PagedBeeper([[self.raw, plain]])
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		items = {i["beeper_chat_id"]: i["avatar_url"] for i in api.unassigned_page()["items"]}
		expected = self.endpoint(beeper_chat_id=self.raw["id"])
		self.assertEqual(items, {self.raw["id"]: expected, plain["id"]: None})
		self.assertEqual(api.preview_chat(self.raw["id"])["avatar_url"], expected)
		self.assertIsNone(api.preview_chat(plain["id"])["avatar_url"])
