import base64
import unittest
from datetime import datetime
from unittest.mock import patch

import frappe

from chatsync import api, sync
from chatsync.messages import upsert
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json, message

INSTALLED = "docarchive" in frappe.get_installed_apps()


@unittest.skipUnless(INSTALLED, "docarchive is not installed")
class TestArchive(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		from docarchive.tests.factories import make_own_company, make_settings

		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		make_settings(default_own_company=make_own_company().name)
		frappe.db.delete("Chat Account", {"user": USER})
		frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		raw = chat_json(number)
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: AssignmentBeeper([raw], {}))
		patcher.start()
		self.addCleanup(patcher.stop)
		self.contact = make_contact(phones=[number])
		self.chat = api.assign_chat(raw["id"], self.contact)
		frappe.db.delete("Chat Message", {"chat": self.chat})
		self.raw = raw
		for day, text in ((1, "first"), (2, "second <b>bold</b><script>x()</script>"), (3, "third")):
			upsert(self.chat, message(raw["id"], 0, text, sortKey=str(1000 + day)))
		names = frappe.get_all("Chat Message", filters={"chat": self.chat}, pluck="name", order_by="creation")
		for day, name in enumerate(names, 1):
			frappe.db.set_value("Chat Message", name, "sent_at", datetime(2026, 1, day, 10, 30))

	def archive(self, *args):
		from chatsync import archive

		return archive.archive_chat(self.chat, *args)

	def test_archive_creates_document_once(self):
		from docarchive.tests.factories import pdf

		with patch("chatsync.archive.render_pdf", return_value=pdf(b"transcript")) as render:
			first = self.archive("2026-01-01", "2026-01-02")
			second = self.archive("2026-01-01", "2026-01-02")
		self.assertEqual(first, second)
		self.assertEqual(
			frappe.db.count("Business Document", {"source_key": f"chat:{self.chat}:2026-01-01:2026-01-02"}), 1
		)
		doc = frappe.get_doc("Business Document", first)
		self.assertEqual(doc.source_system, "chatsync")
		self.assertEqual(str(doc.document_date), "2026-01-02")
		self.assertEqual((doc.source_party_type, doc.source_party_id), ("Contact", self.contact))
		html = render.call_args.args[0]
		self.assertIn("first", html)
		self.assertIn("second <b>bold</b>", html)
		self.assertNotIn("<script>", html)
		self.assertNotIn("third", html)

	def test_changed_content_replaces_with_new_version(self):
		from docarchive.tests.factories import pdf

		with patch("chatsync.archive.render_pdf", return_value=pdf(b"one")):
			first = self.archive()
		with patch("chatsync.archive.render_pdf", return_value=pdf(b"two")):
			second = self.archive()
		self.assertNotEqual(first, second)
		self.assertEqual(frappe.db.get_value("Business Document", second, "replaces"), first)

	def test_real_pdf_is_filed(self):
		name = self.archive()
		self.assertTrue(frappe.get_doc("Business Document", name).file)
		self.assertEqual(self.archive(), name)
		self.assertEqual(str(frappe.db.get_value("Business Document", name, "document_date")), "2026-01-03")

	def test_counterparty_is_assigned_to_contact(self):
		name = self.archive()
		doc = frappe.get_doc("Business Document", name)
		self.assertEqual((doc.counterparty_type, doc.counterparty), ("Contact", self.contact))

	def test_archive_owner_only(self):
		frappe.set_user(make_user("other", ["Chat User"]))
		with self.assertRaises(frappe.PermissionError):
			self.archive()

	def test_archive_needs_archive_role(self):
		with patch.object(frappe, "get_roles", return_value=["Chat User"]):
			with self.assertRaises(frappe.PermissionError):
				self.archive()

	def test_empty_range_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.archive("2025-01-01", "2025-01-02")

	def test_pdf_content_is_base64_of_rendered_pdf(self):
		from docarchive.tests.factories import pdf

		data = pdf(b"x")
		with (
			patch("chatsync.archive.render_pdf", return_value=data),
			patch("docarchive.api.upload_document", return_value={"name": "BD-1"}) as upload,
		):
			self.assertEqual(self.archive(), "BD-1")
		self.assertEqual(base64.b64decode(upload.call_args.kwargs["content_base64"]), data)
