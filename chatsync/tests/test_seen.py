import frappe

from chatsync import api
from chatsync.messages import seen_at, upsert
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_open_chats import ArchivingBeeper, OpenChatsCase, number
from chatsync.tests.test_sync import chat_json, message


class TestSeen(OpenChatsCase):
	def setUp(self):
		super().setUp()
		phone = number()
		self.contact = make_contact(phones=[phone])
		self.raw = chat_json(phone)
		self.use(ArchivingBeeper([self.raw]))
		self.chat = api.assign_chat(self.raw["id"], self.contact)

	def test_seen_is_earliest_receipt_of_own_messages_only(self):
		own = message(self.raw["id"], 0, "hi", isSender=True,
			seen={"a": "2026-11-12T10:05:00.000Z", "b": "2026-11-12T10:01:00.000Z"})
		self.assertEqual(seen_at(own).strftime("%M"), "01")
		self.assertIsNone(seen_at(message(self.raw["id"], 0, "hi", seen={"a": "2026-11-12T10:05:00.000Z"})))
		self.assertIsNone(seen_at(message(self.raw["id"], 0, "hi", isSender=True)))
		self.assertIsNotNone(seen_at(message(self.raw["id"], 0, "hi", isSender=True, seen=True)))

	def test_receipt_arrives_on_a_later_sync_and_reaches_the_view(self):
		raw = message(self.raw["id"], 0, "hi", isSender=True)
		name = upsert(self.chat, raw)
		self.assertIsNone(frappe.db.get_value("Chat Message", name, "seen_at"))
		raw["seen"] = {"them": raw["timestamp"]}
		upsert(self.chat, raw)
		self.assertIsNotNone(frappe.db.get_value("Chat Message", name, "seen_at"))
		(row,) = api.get_messages(self.chat)["messages"]
		self.assertTrue(row["seen_at"])
