from datetime import datetime

import frappe

from chatsync import api
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user


def make_account(user):
	frappe.db.delete("Chat Account", {"user": user})
	return frappe.get_doc(
		{"doctype": "Chat Account", "user": user, "base_url": "http://beeper.test", "access_token": "x"}
	).insert(ignore_permissions=True)


def make_chat(account, contact, network="whatsapp", title="Max"):
	return frappe.get_doc(
		{
			"doctype": "Chat",
			"account": account.name,
			"user": account.user,
			"beeper_chat_id": f"!{tag()}:beeper.local",
			"network": network,
			"title": title,
			"contact": contact,
		}
	).insert(ignore_permissions=True)


def add(chat, sent_at, text="hi", direction="In", name=None):
	doc = frappe.get_doc(
		{
			"doctype": "Chat Message",
			"chat": chat.name,
			"beeper_message_id": tag(),
			"sent_at": sent_at,
			"direction": direction,
			"kind": "Text",
			"text": text,
		}
	)
	if name:
		doc.name = name
		doc.flags.name_set = True
	return doc.insert(ignore_permissions=True, set_name=name).name


class TestContactMessages(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		self.user = make_user("merged", ["Chat User", "System Manager"])
		self.account = make_account(self.user)
		self.contact = make_contact()
		self.whatsapp = make_chat(self.account, self.contact)
		self.signal = make_chat(self.account, self.contact, network="signal")

	def texts(self, result):
		return [m["text"] for m in result["messages"]]

	def test_merges_chats_in_time_order(self):
		add(self.whatsapp, datetime(2026, 3, 1, 9), "wa 1")
		add(self.signal, datetime(2026, 3, 1, 10), "sig 1")
		add(self.whatsapp, datetime(2026, 3, 1, 11), "wa 2", direction="Out")
		frappe.set_user(self.user)
		result = api.get_contact_messages(self.contact)
		self.assertEqual(self.texts(result), ["wa 1", "sig 1", "wa 2"])
		self.assertEqual(
			[(m["chat"], m["network"]) for m in result["messages"]],
			[
				(self.whatsapp.name, "whatsapp"),
				(self.signal.name, "signal"),
				(self.whatsapp.name, "whatsapp"),
			],
		)
		self.assertEqual(result["messages"][1]["network_label"], "Signal")
		self.assertEqual(result["messages"][0]["html"], "wa 1")
		self.assertEqual(result["contact"], self.contact)
		self.assertEqual(result["contact_name"], frappe.db.get_value("Contact", self.contact, "first_name"))
		self.assertEqual({c["name"] for c in result["chats"]}, {self.whatsapp.name, self.signal.name})
		self.assertFalse(result["has_more"])

	def test_paging_with_ties_across_chats(self):
		moment = datetime(2026, 3, 1, 9)
		names = [
			add(self.whatsapp, moment, "a", name=f"zz-{tag()}-a"),
			add(self.signal, moment, "b", name=f"zz-{tag()}-b"),
			add(self.whatsapp, moment, "c", name=f"zz-{tag()}-c"),
			add(self.signal, datetime(2026, 3, 1, 10), "d"),
		]
		expected = [n for n in sorted(names[:3])] + [names[3]]
		frappe.set_user(self.user)
		first = api.get_contact_messages(self.contact, limit=2)
		self.assertTrue(first["has_more"])
		second = api.get_contact_messages(self.contact, before=first["messages"][0]["name"], limit=2)
		self.assertFalse(second["has_more"])
		loaded = [m["name"] for m in second["messages"] + first["messages"]]
		self.assertEqual(loaded, expected)

	def test_owner_only(self):
		add(self.whatsapp, datetime(2026, 3, 1, 9), "mine")
		other = make_user("merged-other", ["Chat User", "System Manager"])
		foreign = make_chat(make_account(other), self.contact, network="signal")
		add(foreign, datetime(2026, 3, 1, 10), "theirs")
		frappe.set_user(self.user)
		result = api.get_contact_messages(self.contact)
		self.assertEqual(self.texts(result), ["mine"])
		self.assertNotIn(foreign.name, [c["name"] for c in result["chats"]])
		frappe.set_user("Administrator")
		self.assertNotIn("theirs", self.texts(api.get_contact_messages(self.contact)))

	def test_default_chat_is_latest_incoming(self):
		add(self.signal, datetime(2026, 3, 1, 9), "in on signal")
		add(self.whatsapp, datetime(2026, 3, 1, 10), "out on whatsapp", direction="Out")
		frappe.set_user(self.user)
		result = api.get_contact_messages(self.contact)
		self.assertEqual(result["default_chat"], self.signal.name)
		chats = {c["name"]: c for c in result["chats"]}
		self.assertEqual(str(chats[self.signal.name]["last_incoming_at"]), "2026-03-01 09:00:00")
		self.assertIsNone(chats[self.whatsapp.name]["last_incoming_at"])
		self.assertEqual(chats[self.signal.name]["network_label"], "Signal")

	def test_default_chat_falls_back_to_latest_chat(self):
		add(self.signal, datetime(2026, 3, 1, 9), "x", direction="Out")
		add(self.whatsapp, datetime(2026, 3, 1, 10), "y", direction="Out")
		frappe.db.set_value("Chat", self.whatsapp.name, "last_message_at", datetime(2026, 3, 1, 10))
		frappe.db.set_value("Chat", self.signal.name, "last_message_at", datetime(2026, 3, 1, 9))
		frappe.set_user(self.user)
		self.assertEqual(api.get_contact_messages(self.contact)["default_chat"], self.whatsapp.name)

	def test_contact_must_be_readable(self):
		frappe.set_user(make_user("no-contact", ["Chat User"]))
		with self.assertRaises(frappe.PermissionError):
			api.get_contact_messages(self.contact)

	def test_chat_route_resolves_to_contact(self):
		add(self.whatsapp, datetime(2026, 3, 1, 9), "wa")
		add(self.signal, datetime(2026, 3, 1, 10), "sig")
		frappe.set_user(self.user)
		contact = api.get_messages(self.whatsapp.name)["chat"]["contact"]
		self.assertEqual(contact, self.contact)
		self.assertEqual(self.texts(api.get_contact_messages(contact)), ["wa", "sig"])

	def test_get_contact_messages_is_get(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.get_contact_messages], ["GET"])
