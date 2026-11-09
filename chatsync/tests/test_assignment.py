from unittest.mock import patch

import frappe

from chatsync import api, sync
from chatsync.beeper import BeeperError
from chatsync.tests.helpers import ChatsyncTestCase, tag, use_hooks
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, FakeBeeper, chat_json, message

LOOKUP = "Chat Identifier"


class AssignmentBeeper(FakeBeeper):
	def chat(self, chat_id):
		return next(c for c in self.chat_items if c["id"] == chat_id)

	def search_chats(self, query, limit=20, kind="single"):
		return [
			c
			for c in self.chat_items
			if query.lower() in c["title"].lower() and (kind == "any" or c.get("type") != "group")
		]


class TestAssignment(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		self.number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"

	def use(self, *chats, history=None):
		fake = AssignmentBeeper(list(chats), history or {})
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		return fake

	def test_assign_imports_history_and_keeps_phones(self):
		chat = chat_json(self.number)
		self.use(chat, history={chat["id"]: [[message(chat["id"], 1, "old news")]]})
		contact = make_contact(phones=["+49 30 1234567"])
		phones = [p.phone for p in frappe.get_doc("Contact", contact).phone_nos]
		name = api.assign_chat(chat["id"], contact)
		doc = frappe.get_doc("Chat", name)
		self.assertEqual(
			(doc.contact, doc.user, doc.history_requested, doc.history_complete), (contact, USER, 1, 1)
		)
		self.assertEqual(frappe.get_all("Chat Message", filters={"chat": name}, pluck="text"), ["old news"])
		contact_doc = frappe.get_doc("Contact", contact)
		self.assertEqual([p.phone for p in contact_doc.phone_nos], phones)
		self.assertEqual([r.beeper_chat_id for r in contact_doc.chat_identifiers], [chat["id"]])
		self.assertFalse(contact_doc.chat_identifiers[0].excluded)

	def test_assignment_does_not_save_the_contact(self):
		chat = chat_json(self.number)
		self.use(chat)
		first, second = make_contact(), make_contact()
		frappe.db.set_value(
			"Contact", [first, second], "modified", "2000-01-01 00:00:00", update_modified=False
		)
		with patch.object(
			frappe.get_doc("Contact", first).__class__, "save", side_effect=AssertionError("save")
		):
			name = api.assign_chat(chat["id"], first)
			name = api.reassign_chat(name, second)
			api.unassign_chat(name)
		for contact in (first, second):
			self.assertEqual(str(frappe.db.get_value("Contact", contact, "modified")), "2000-01-01 00:00:00")
		rows = frappe.get_doc("Contact", second).chat_identifiers
		self.assertEqual(
			[(r.beeper_chat_id, r.excluded, r.parentfield) for r in rows],
			[(chat["id"], 1, "chat_identifiers")],
		)

	def test_unassign_deletes_and_excludes(self):
		chat = chat_json(self.number)
		fake = self.use(chat, history={chat["id"]: [[message(chat["id"], 1)]]})
		contact = make_contact(phones=[self.number])
		name = api.assign_chat(chat["id"], contact)
		api.unassign_chat(name)
		fake.calls.clear()
		self.assertFalse(frappe.db.exists("Chat", name))
		self.assertFalse(frappe.db.exists("Chat Message", {"chat": name}))
		rows = frappe.get_doc("Contact", contact).chat_identifiers
		self.assertEqual([(r.beeper_chat_id, bool(r.excluded)) for r in rows], [(chat["id"], True)])
		sync.sync_account(self.account.name)
		self.assertEqual(frappe.get_all("Chat", filters={"account": self.account.name}), [])
		self.assertEqual(fake.calls, [])

	def test_reassign_moves_chat(self):
		chat = chat_json(self.number)
		self.use(chat, history={chat["id"]: [[message(chat["id"], 1)]]})
		first, second = make_contact(), make_contact()
		api.assign_chat(chat["id"], first)
		api.reassign_chat(frappe.db.get_value("Chat", {"beeper_chat_id": chat["id"]}), second)
		self.assertEqual(frappe.db.get_value("Chat", {"beeper_chat_id": chat["id"]}, "contact"), second)
		self.assertEqual(frappe.get_doc("Contact", first).chat_identifiers, [])
		self.assertFalse(frappe.get_doc("Contact", second).chat_identifiers[0].excluded)
		sync.sync_account(self.account.name)
		self.assertEqual(frappe.db.get_value("Chat", {"beeper_chat_id": chat["id"]}, "contact"), second)

	def test_unassigned_lists_unmatched_direct_chats_and_groups(self):
		matched = chat_json(self.number)
		make_contact(phones=[self.number])
		free = chat_json(f"+4916{int(tag()[:7], 16) % 10**8:08d}")
		old = chat_json(f"+4917{int(tag()[:7], 16) % 10**8:08d}", last_days=30)
		group = chat_json(kind="group", others=2)
		shared = f"+4918{int(tag()[:7], 16) % 10**8:08d}"
		make_contact(phones=[shared])
		make_contact(phones=[shared])
		ambiguous = chat_json(shared)
		excluded = chat_json(f"+4919{int(tag()[:7], 16) % 10**8:08d}")
		make_contact(identifiers=[{"network": "whatsapp", "beeper_chat_id": excluded["id"], "excluded": 1}])
		self.use(matched, free, group, ambiguous, excluded, old)
		before = frappe.db.count("Chat")
		result = {r["beeper_chat_id"]: r for r in api.unassigned_chats(days=14)}
		self.assertEqual(set(result), {free["id"], ambiguous["id"], group["id"]})
		self.assertTrue(result[group["id"]]["is_group"])
		self.assertTrue(result[ambiguous["id"]]["ambiguous"])
		self.assertFalse(result[free["id"]]["ambiguous"])
		self.assertEqual(frappe.db.count("Chat"), before)

	def test_unassigned_chats_use_short_timeout_and_raise_beeper_errors(self):
		created = []

		class Down(AssignmentBeeper):
			def chats(self, cursor=None):
				raise BeeperError(0, "ConnectionError")

		def client(*args, **kwargs):
			created.append(kwargs.get("timeout"))
			return Down([], {})

		frappe.local.message_log = []
		with patch.object(sync, "BeeperClient", client):
			with self.assertRaises(BeeperError):
				api.find_unassigned_chats(USER)
			self.assertEqual(frappe.message_log, [])
			with self.assertRaises(frappe.ValidationError):
				api.unassigned_chats()
		self.assertEqual(created, [api.UNASSIGNED_TIMEOUT] * 2)

	def test_unassigned_chats_without_account_is_empty(self):
		frappe.db.set_value("Chat Account", self.account.name, "enabled", 0)
		self.assertEqual(api.find_unassigned_chats(USER), [])

	def test_ignored_chat_leaves_unassigned_list(self):
		kept, ignored = chat_json(self.number), chat_json(f"+4916{int(tag()[:7], 16) % 10**8:08d}")
		self.use(kept, ignored)
		api.ignore_chat(ignored["id"])
		api.ignore_chat(ignored["id"])
		self.assertEqual([r["beeper_chat_id"] for r in api.find_unassigned_chats(USER)], [kept["id"]])
		self.assertEqual(api.ignored_chats(USER), {ignored["id"]})

	def test_ignore_is_per_user(self):
		chat = chat_json(self.number)
		self.use(chat)
		other = self.other_user_with_account()
		frappe.set_user(other)
		api.ignore_chat(chat["id"])
		frappe.set_user("Administrator")
		self.assertEqual([r["beeper_chat_id"] for r in api.find_unassigned_chats(USER)], [chat["id"]])

	def test_unassigned_chat_names_existing_private_contact(self):
		use_hooks(self, chatsync_contact_filter=["chatsync.tests.test_sync.qualifies_none"])
		chat = chat_json(self.number)
		self.use(chat)
		private = make_contact(phones=[self.number])
		(item,) = api.find_unassigned_chats(USER)
		self.assertEqual((item["existing_contact"], item["ambiguous"]), (private, False))
		make_contact(phones=[self.number])
		(item,) = api.find_unassigned_chats(USER)
		self.assertIsNone(item["existing_contact"])

	def test_search_chats_returns_direct_chats(self):
		chat = chat_json(self.number)
		self.use(chat, chat_json(kind="group", others=2))
		result = api.search_chats("other")
		self.assertEqual([r["beeper_chat_id"] for r in result], [chat["id"]])
		self.assertEqual(result[0]["phone"], self.number)

	def test_create_contact_for_chat_prefills_phone(self):
		chat = {**chat_json(self.number), "title": "Erika Muster Mann"}
		self.use(chat, history={chat["id"]: [[message(chat["id"], 1)]]})
		name = api.create_contact_for_chat(chat["id"])
		contact = frappe.get_doc("Contact", name)
		self.assertEqual((contact.first_name, contact.last_name), ("Erika", "Muster Mann"))
		self.assertEqual([p.phone for p in contact.phone_nos], [self.number])
		self.assertEqual(frappe.db.get_value("Chat", {"beeper_chat_id": chat["id"]}, "contact"), name)

	def other_user_with_account(self):
		other = make_user("b", ["Chat User"])
		self.addCleanup(frappe.delete_doc, "User", other, force=True, ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "Chat Account", "user": other, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		return other

	def test_assign_other_users_chat_forbidden(self):
		chat = chat_json(self.number)
		self.use(chat)
		contact = make_contact()
		name = api.assign_chat(chat["id"], contact)
		other = self.other_user_with_account()
		frappe.share.add("Contact", contact, other, read=1, flags={"ignore_share_permission": True})
		frappe.set_user(other)
		self.assertTrue(frappe.has_permission("Contact", doc=contact, user=other))
		with self.assertRaises(frappe.PermissionError):
			api.unassign_chat(name)
		with self.assertRaises(frappe.PermissionError):
			api.reassign_chat(name, contact)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Chat", name, ["user", "contact"]), (USER, contact))
		self.assertEqual(frappe.db.count("Chat", {"beeper_chat_id": chat["id"]}), 1)

	def test_reader_who_is_not_owner_cannot_change_chat(self):
		other = self.other_user_with_account()
		account = frappe.db.get_value("Chat Account", {"user": other})
		chat = frappe.get_doc(
			{"doctype": "Chat", "account": account, "beeper_chat_id": f"!{tag()}:beeper.local"}
		).insert(ignore_permissions=True)
		self.assertTrue(frappe.has_permission("Chat", "read", doc=chat.name))
		with self.assertRaises(frappe.PermissionError):
			api.unassign_chat(chat.name)
		self.assertTrue(frappe.db.exists("Chat", chat.name))

	def test_assign_to_other_contact_replaces_rows(self):
		chat = chat_json(self.number)
		self.use(chat)
		first, second = make_contact(), make_contact()
		name = api.assign_chat(chat["id"], first)
		self.assertEqual(api.assign_chat(chat["id"], second), name)
		self.assertEqual(frappe.get_doc("Contact", first).chat_identifiers, [])
		self.assertEqual(frappe.db.get_value("Chat", name, "contact"), second)
		sync.sync_account(self.account.name)
		self.assertEqual(frappe.db.get_value("Chat", name, "contact"), second)

	def test_reassign_beats_old_contacts_phone(self):
		chat = chat_json(self.number)
		self.use(chat)
		old, new = make_contact(phones=[self.number]), make_contact()
		name = api.assign_chat(chat["id"], old)
		api.reassign_chat(name, new)
		sync.sync_account(self.account.name)
		self.assertEqual(frappe.db.get_value("Chat", {"beeper_chat_id": chat["id"]}, "contact"), new)

	def test_unreadable_contact_refused(self):
		chat = chat_json(self.number)
		self.use(chat)
		contact = make_contact()
		frappe.set_user(self.other_user_with_account())
		with self.assertRaises(frappe.PermissionError):
			api.assign_chat(chat["id"], contact)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.count("Chat", {"beeper_chat_id": chat["id"]}), 0)
