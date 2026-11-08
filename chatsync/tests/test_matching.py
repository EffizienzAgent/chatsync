import frappe
from frappe.tests import IntegrationTestCase

from chatsync.matching import Counterpart, Match, counterpart, match, phone_index
from chatsync.phone import normalize
from chatsync.tests.helpers import tag, use_hooks

USER = "Administrator"


def only_marked(contact):
	return frappe.db.get_value("Contact", contact, "first_name").startswith("marked")


def make_contact(prefix="plain", phones=(), identifiers=()):
	doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": f"{prefix}-{tag()}",
			"phone_nos": [{"phone": phone} for phone in phones],
			"chat_identifiers": list(identifiers),
		}
	).insert(ignore_permissions=True)
	return doc.name


def cp(phone=None, chat_id=None, username=None, participant_id="@x:beeper.local"):
	return Counterpart("whatsapp", chat_id or f"!{tag()}:beeper.local", participant_id, phone, username, None)


class TestMatching(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"

	def test_formats_match(self):
		contact = make_contact(phones=[self.number[:3] + " " + self.number[3:6] + " " + self.number[6:]])
		self.assertEqual(match(USER, cp(phone=self.number)), Match(contact, False, False))
		self.assertEqual(normalize("01512345678", "DE"), "+491512345678")
		self.assertEqual(normalize("0049 1512345678", "DE"), "+491512345678")
		self.assertIsNone(normalize("abc", "DE"))

	def test_ambiguous_number_not_linked(self):
		make_contact(phones=[self.number])
		make_contact(phones=[self.number])
		self.assertEqual(match(USER, cp(phone=self.number)), Match(None, True, False))

	def test_filter_hook_excludes_private(self):
		use_hooks(self, chatsync_contact_filter=["chatsync.tests.test_matching.only_marked"])
		make_contact("plain", phones=[self.number])
		self.assertEqual(match(USER, cp(phone=self.number)), Match(None, False, False))
		marked = make_contact("marked", phones=[self.number])
		self.assertEqual(match(USER, cp(phone=self.number)), Match(marked, False, False))

	def test_identifier_beats_phone_and_ignores_filter(self):
		use_hooks(self, chatsync_contact_filter=["chatsync.tests.test_matching.only_marked"])
		chat_id = f"!{tag()}:beeper.local"
		linked = make_contact(
			"plain", identifiers=[{"network": "whatsapp", "beeper_chat_id": chat_id, "identifier": "x"}]
		)
		make_contact("marked", phones=[self.number])
		self.assertEqual(match(USER, cp(phone=self.number, chat_id=chat_id)), Match(linked, False, False))

	def test_identifier_by_username(self):
		name = tag()
		contact = make_contact(identifiers=[{"network": "whatsapp", "identifier": name}])
		self.assertEqual(match(USER, cp(username=name)), Match(contact, False, False))

	def test_excluded_identifier(self):
		chat_id = f"!{tag()}:beeper.local"
		make_contact(
			phones=[self.number],
			identifiers=[{"network": "whatsapp", "beeper_chat_id": chat_id, "excluded": 1}],
		)
		self.assertEqual(match(USER, cp(phone=self.number, chat_id=chat_id)), Match(None, False, True))

	def test_exclusion_does_not_leak_to_other_chat(self):
		name = tag()
		excluded_chat = f"!{tag()}:beeper.local"
		contact = make_contact(
			identifiers=[
				{"network": "whatsapp", "beeper_chat_id": excluded_chat, "identifier": name, "excluded": 1},
				{"network": "whatsapp", "identifier": name},
			]
		)
		self.assertEqual(match(USER, cp(username=name, chat_id=excluded_chat)), Match(None, False, True))
		self.assertEqual(match(USER, cp(username=name)), Match(contact, False, False))

	def test_prebuilt_index(self):
		contact = make_contact(phones=[self.number])
		self.assertEqual(match(USER, cp(phone=self.number), phone_index()), Match(contact, False, False))

	def test_unreadable_contact_ignored(self):
		chat_id = f"!{tag()}:beeper.local"
		make_contact(identifiers=[{"network": "whatsapp", "beeper_chat_id": chat_id}])
		guest = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"nobody-{tag()}@chatsync.test",
				"first_name": "n",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		self.assertEqual(match(guest.name, cp(chat_id=chat_id)), Match(None, False, False))

	def test_group_self_and_merged_chats_ignored(self):
		other = {"id": "@whatsapp_1:beeper.local", "phoneNumber": "+491512345678", "isSelf": False}
		me = {"id": "@me:beeper.com", "isSelf": True}
		base = {
			"id": "!x:beeper.local",
			"accountID": "whatsapp.4915",
			"type": "single",
			"network": "whatsapp",
		}
		self.assertEqual(
			counterpart({**base, "participants": {"items": [me, other]}}),
			Counterpart("whatsapp", "!x:beeper.local", other["id"], "+491512345678", None, None),
		)
		self.assertIsNotNone(counterpart({**base, "participants": [me, other]}))
		self.assertIsNone(counterpart({**base, "type": "group", "participants": {"items": [me, other]}}))
		self.assertIsNone(counterpart({**base, "participants": {"items": [me]}}))
		self.assertIsNone(
			counterpart({**base, "network": "matrix", "merge": {}, "participants": {"items": [me, other]}})
		)
