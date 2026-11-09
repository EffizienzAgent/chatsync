import json

import frappe

from chatsync import api
from chatsync.messages import upsert
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_open_chats import ArchivingBeeper, OpenChatsCase, number
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import chat_json, message

REACT = "chatsync.api.react"


class ReactingBeeper(ArchivingBeeper):
	def __init__(self, chats):
		super().__init__(chats)
		self.reactions = []

	def react(self, chat_id, message_id, key):
		self.reactions.append(("add", chat_id, message_id, key))
		return {"success": True}

	def unreact(self, chat_id, message_id, key):
		self.reactions.append(("remove", chat_id, message_id, key))
		return {"success": True}


def reaction(participant, key, **extra):
	return {"id": f"{participant}{key}", "reactionKey": key, "participantID": participant, **extra}


class TestReactions(OpenChatsCase):
	def setUp(self):
		super().setUp()
		phone = number()
		self.contact = make_contact(phones=[phone])
		self.raw = chat_json(phone)
		self.them = self.raw["participants"]["items"][1]["id"]
		self.fake = self.use(ReactingBeeper([self.raw]))
		self.chat = api.assign_chat(self.raw["id"], self.contact)

	def stored(self, name):
		return json.loads(frappe.db.get_value("Chat Message", name, "reactions") or "null")

	def test_upsert_stores_updates_and_clears_reactions(self):
		raw = message(self.raw["id"], 0, "hi", reactions=[reaction(self.them, "👍")])
		name = upsert(self.chat, raw)
		self.assertEqual(self.stored(name), [{"key": "👍", "participant_id": self.them, "is_self": False}])
		raw["reactions"].append(reaction("@me:beeper.local", "smiling-face", emoji=False))
		modified = frappe.db.get_value("Chat Message", name, "modified")
		upsert(self.chat, raw)
		self.assertEqual(
			[(r["key"], r["is_self"]) for r in self.stored(name)], [("👍", False), ("smiling-face", True)]
		)
		self.assertNotEqual(frappe.db.get_value("Chat Message", name, "modified"), modified)
		modified = frappe.db.get_value("Chat Message", name, "modified")
		upsert(self.chat, raw)
		self.assertEqual(frappe.db.get_value("Chat Message", name, "modified"), modified)
		raw["reactions"] = []
		upsert(self.chat, raw)
		self.assertIsNone(self.stored(name))

	def test_reaction_events_stay_skipped(self):
		self.assertIsNone(upsert(self.chat, message(self.raw["id"], 0, "", type="REACTION")))

	def test_messages_carry_grouped_reactions(self):
		reactions = [reaction(self.them, "👍"), reaction("@me:beeper.local", "👍"), reaction(self.them, "❤️")]
		upsert(self.chat, message(self.raw["id"], 0, "hi", reactions=reactions))
		expected = [{"key": "👍", "count": 2, "mine": True}, {"key": "❤️", "count": 1, "mine": False}]
		(row,) = api.get_messages(self.chat)["messages"]
		self.assertEqual(row["reactions"], expected)
		(row,) = api.get_contact_messages(self.contact)["messages"]
		self.assertEqual(row["reactions"], expected)

	def test_react_adds_and_removes_through_beeper(self):
		raw = message(self.raw["id"], 0, "hi", reactions=[reaction(self.them, "👍")])
		name = upsert(self.chat, raw)
		self.browser(REACT)
		result = api.react(name, "❤️")
		self.assertEqual(self.fake.reactions, [("add", self.raw["id"], raw["id"], "❤️")])
		self.assertEqual(
			result["reactions"],
			[{"key": "👍", "count": 1, "mine": False}, {"key": "❤️", "count": 1, "mine": True}],
		)
		result = api.react(name, "❤️", remove=1)
		self.assertEqual(self.fake.reactions[-1], ("remove", self.raw["id"], raw["id"], "❤️"))
		self.assertEqual(result["reactions"], [{"key": "👍", "count": 1, "mine": False}])

	def test_react_refuses_token_auth(self):
		name = upsert(self.chat, message(self.raw["id"], 0, "hi"))
		self.browser(REACT, authorization="token a:b")
		with self.assertRaises(frappe.PermissionError):
			api.react(name, "👍")
		self.assertEqual(self.fake.reactions, [])

	def test_react_refuses_empty_keys(self):
		name = upsert(self.chat, message(self.raw["id"], 0, "hi"))
		self.browser(REACT)
		with self.assertRaises(frappe.ValidationError):
			api.react(name, " ")
		self.assertEqual(self.fake.reactions, [])

	def test_react_refuses_another_users_chat(self):
		name = upsert(self.chat, message(self.raw["id"], 0, "hi"))
		frappe.set_user(make_user("react", ["Chat User"]))
		self.browser(REACT)
		with self.assertRaises(frappe.PermissionError):
			api.react(name, "👍")
		self.assertEqual(self.fake.reactions, [])
