from unittest.mock import patch

import frappe
from frappe.utils import get_datetime

from chatsync import api, sync
from chatsync.messages import to_site_time
from chatsync.tests.helpers import ChatsyncTestCase, tag, use_hooks
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_chat_view import fake_request
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json, message

DONE = "chatsync.api.set_chat_done"
CALLS = []


def remember_done(**kwargs):
	CALLS.append(kwargs)


def number(prefix="15"):
	return f"+49{prefix}{int(tag()[:7], 16) % 10**8:08d}"


class ArchivingBeeper(AssignmentBeeper):
	def __init__(self, chats, messages=None):
		super().__init__(chats, messages or {})
		self.archived = []
		self.fetched = []

	def chat(self, chat_id):
		self.fetched.append(chat_id)
		return super().chat(chat_id)

	def archive(self, chat_id, archived=True):
		self.archived.append((chat_id, archived))
		self.chat(chat_id)["isArchived"] = archived


class OpenChatsCase(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)

	def use(self, fake):
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		return fake

	def browser(self, cmd=DONE, **kwargs):
		patcher = patch.object(frappe.local, "request", fake_request(**kwargs), create=True)
		patcher.start()
		self.addCleanup(patcher.stop)
		session = patch.dict(frappe.session.data, {"csrf_token": "csrf-token-1"})
		session.start()
		self.addCleanup(session.stop)
		form = patch.dict(frappe.local.form_dict, {"cmd": cmd})
		form.start()
		self.addCleanup(form.stop)

	def stored(self, raw):
		return frappe.db.get_value("Chat", {"beeper_chat_id": raw["id"]}, ["*"], as_dict=True)


class TestChatState(OpenChatsCase):
	def test_sync_stores_archive_unread_and_last_directions(self):
		phone = number()
		make_contact(phones=[phone])
		raw = {**chat_json(phone), "isArchived": True, "unreadCount": 2}
		incoming = message(raw["id"], 0, "question")
		outgoing = message(raw["id"], 1, "answer", isSender=True)
		fake = self.use(ArchivingBeeper([raw], {raw["id"]: [[incoming, outgoing]]}))
		sync.sync_account(self.account.name)
		row = self.stored(raw)
		self.assertEqual((row.beeper_archived, row.unread_count), (1, 2))
		self.assertEqual(get_datetime(row.last_incoming_at), to_site_time(incoming["timestamp"]))
		self.assertEqual(get_datetime(row.last_outgoing_at), to_site_time(outgoing["timestamp"]))
		raw.update(isArchived=False, unreadCount=0)
		sync.sync_account(self.account.name)
		self.assertEqual(self.stored(raw).beeper_archived, 0)
		self.assertEqual(fake.archived, [])

	def test_merged_member_takes_the_parents_archive_state(self):
		phone = number()
		make_contact(phones=[phone])
		member = {**chat_json(phone), "isArchived": False}
		parent = {**chat_json(), "merge": {"chatIDs": [member["id"]]}, "isArchived": True}
		fake = ArchivingBeeper([parent, member], {member["id"]: [[message(member["id"], 0)]]})
		fake.chats = lambda cursor=None: ([parent], None)
		self.use(fake)
		sync.sync_account(self.account.name)
		self.assertEqual(self.stored(member).beeper_archived, 1)

	def test_open_chat_outside_the_window_is_refreshed_from_beeper(self):
		phone = number()
		make_contact(phones=[phone])
		member = {**chat_json(phone), "isArchived": False}
		fake = self.use(ArchivingBeeper([member], {member["id"]: [[message(member["id"], 0)]]}))
		sync.sync_account(self.account.name)
		parent = {**chat_json(), "merge": {"chatIDs": [member["id"]]}, "isArchived": True}
		member["mergedIntoChatID"] = parent["id"]
		fake.chat_items.append(parent)
		fake.chats = lambda cursor=None: ([], None)
		fake.fetched.clear()
		sync.sync_account(self.account.name)
		self.assertEqual(self.stored(member).beeper_archived, 1)
		self.assertEqual(fake.fetched, [member["id"], parent["id"]])
		fake.fetched.clear()
		sync.sync_account(self.account.name)
		self.assertEqual(fake.fetched, [])


class TestSetChatDone(OpenChatsCase):
	def setUp(self):
		super().setUp()
		CALLS.clear()
		self.phone = number()
		self.contact = make_contact(phones=[self.phone])
		self.raw = {**chat_json(self.phone), "isArchived": False}
		self.fake = self.use(ArchivingBeeper([self.raw]))
		self.chat = api.assign_chat(self.raw["id"], self.contact)

	def test_archives_and_reopens_a_stored_chat(self):
		use_hooks(self, chatsync_chat_done=["chatsync.tests.test_open_chats.remember_done"])
		self.browser()
		result = api.set_chat_done(chat=self.chat)
		self.assertEqual(self.fake.archived, [(self.raw["id"], True)])
		self.assertEqual(frappe.db.get_value("Chat", self.chat, "beeper_archived"), 1)
		self.assertEqual(result, {"chat": self.chat, "beeper_chat_id": self.raw["id"], "done": True})
		api.set_chat_done(chat=self.chat, done=0)
		self.assertEqual(self.fake.archived[-1], (self.raw["id"], False))
		self.assertEqual(frappe.db.get_value("Chat", self.chat, "beeper_archived"), 0)
		self.assertEqual(
			CALLS,
			[
				{"user": USER, "beeper_chat_id": self.raw["id"], "done": True},
				{"user": USER, "beeper_chat_id": self.raw["id"], "done": False},
			],
		)

	def test_merged_member_archives_the_merged_chat(self):
		parent = {**chat_json(), "merge": {"chatIDs": [self.raw["id"]]}, "isArchived": False}
		self.raw["mergedIntoChatID"] = parent["id"]
		self.fake.chat_items.append(parent)
		self.browser()
		api.set_chat_done(chat=self.chat)
		self.assertEqual(self.fake.archived, [(parent["id"], True)])
		self.assertEqual(frappe.db.get_value("Chat", self.chat, "beeper_archived"), 1)

	def test_refuses_token_auth(self):
		self.browser(authorization="token a:b")
		with self.assertRaises(frappe.PermissionError):
			api.set_chat_done(chat=self.chat)
		self.assertEqual(self.fake.archived, [])

	def test_refuses_calls_from_other_endpoints(self):
		self.browser(cmd="chatsync.api.send_message")
		with self.assertRaises(frappe.PermissionError):
			api.set_chat_done(chat=self.chat)
		self.assertEqual(self.fake.archived, [])

	def test_refuses_another_users_chat(self):
		other = make_user("done", ["Chat User"])
		frappe.set_user(other)
		self.browser()
		with self.assertRaises(frappe.PermissionError):
			api.set_chat_done(chat=self.chat)
		with self.assertRaises(frappe.ValidationError):
			api.set_chat_done(beeper_chat_id=self.raw["id"])
		self.assertEqual(self.fake.archived, [])

	def test_archives_an_unassigned_direct_chat_only(self):
		loose = {**chat_json(number("16")), "isArchived": False}
		group = chat_json(kind="group", others=2)
		self.fake.chat_items += [loose, group]
		self.browser()
		self.assertEqual(api.set_chat_done(beeper_chat_id=loose["id"])["done"], True)
		self.assertEqual(self.fake.archived, [(loose["id"], True)])
		with self.assertRaises(frappe.ValidationError):
			api.set_chat_done(beeper_chat_id=group["id"])
		with self.assertRaises(frappe.ValidationError):
			api.set_chat_done()


class TestOpenAssignedChats(OpenChatsCase):
	def chat_for(self, contact, phone, **state):
		raw = {**chat_json(phone), "isArchived": False}
		self.fake.chat_items.append(raw)
		chat = api.assign_chat(raw["id"], contact)
		if state:
			frappe.db.set_value("Chat", chat, state, update_modified=False)
		return chat

	def test_needs_reply_first_grouped_by_contact(self):
		self.fake = self.use(ArchivingBeeper([]))
		anna, ben, cleo = (make_contact(phones=[number(p)]) for p in ("15", "16", "17"))
		for contact in (anna, ben, cleo):
			frappe.db.set_value("Contact", contact, "first_name", f"Name {contact}")
		answered = self.chat_for(
			anna,
			number(),
			last_incoming_at="2026-11-01 10:00",
			last_outgoing_at="2026-11-01 11:00",
			last_message_at="2026-11-01 11:00",
		)
		waiting = self.chat_for(
			ben,
			number(),
			last_incoming_at="2026-10-30 09:00",
			last_message_at="2026-10-30 09:00",
			unread_count=1,
		)
		second = self.chat_for(
			ben,
			number(),
			last_incoming_at="2026-10-29 09:00",
			last_outgoing_at="2026-10-29 10:00",
			last_message_at="2026-10-29 10:00",
		)
		self.chat_for(cleo, number(), beeper_archived=1, last_incoming_at="2026-11-02 09:00")
		rows = api.open_assigned_chats(USER)
		self.assertEqual([r["contact"] for r in rows], [ben, anna])
		first = rows[0]
		self.assertEqual((first["chat"], sorted(first["chats"])), (waiting, sorted([waiting, second])))
		self.assertEqual((first["needs_reply"], first["unread_count"]), (True, 1))
		self.assertEqual(first["contact_name"], f"Name {ben}")
		self.assertEqual(first["networks"], ["WhatsApp"])
		self.assertEqual(str(first["last_incoming_at"]), "2026-10-30 09:00:00")
		self.assertFalse(rows[1]["needs_reply"])
		self.assertEqual(rows[1]["chats"], [answered])

	def test_other_users_and_disabled_accounts_stay_out(self):
		self.fake = self.use(ArchivingBeeper([]))
		self.chat_for(make_contact(phones=[number()]), number(), last_incoming_at="2026-11-01 10:00")
		self.assertEqual(api.open_assigned_chats("Guest"), [])
		frappe.db.set_value("Chat Account", self.account.name, "enabled", 0)
		self.assertEqual(api.open_assigned_chats(USER), [])

	def test_preview_is_plain_text_of_the_newest_message(self):
		self.fake = self.use(ArchivingBeeper([]))
		chat = self.chat_for(make_contact(phones=[number()]), number())
		from chatsync.messages import upsert

		raw_id = frappe.db.get_value("Chat", chat, "beeper_chat_id")
		upsert(chat, message(raw_id, 1, "older"))
		upsert(chat, message(raw_id, 0, "*neu*"))
		sync.update_activity(chat)
		(row,) = api.open_assigned_chats(USER)
		self.assertEqual((row["preview"], row["needs_reply"]), ("neu", True))


class TestUnassignedState(OpenChatsCase):
	def test_items_carry_archived_and_needs_reply(self):
		waiting = {**chat_json(number()), "isArchived": False, "preview": {"isSender": False}}
		answered = {**chat_json(number("16")), "isArchived": True, "preview": {"isSender": True}}
		unread = {**chat_json(number("17")), "unreadCount": 3}
		self.use(ArchivingBeeper([waiting, answered, unread]))
		items = {i["beeper_chat_id"]: i for i in api.find_unassigned_chats(USER)}
		self.assertEqual(
			(items[waiting["id"]]["archived"], items[waiting["id"]]["needs_reply"]), (False, True)
		)
		self.assertEqual(
			(items[answered["id"]]["archived"], items[answered["id"]]["needs_reply"]), (True, False)
		)
		self.assertEqual(items[unread["id"]]["needs_reply"], True)


class TestChatViewState(OpenChatsCase):
	def test_messages_and_conversations_carry_archived(self):
		phone = number()
		contact = make_contact(phones=[phone])
		raw = {**chat_json(phone), "isArchived": False}
		self.use(ArchivingBeeper([raw]))
		chat = api.assign_chat(raw["id"], contact)
		frappe.db.set_value("Chat", chat, "beeper_archived", 1)
		self.assertTrue(api.get_messages(chat)["chat"]["archived"])
		self.assertTrue(api.get_contact_messages(contact)["chats"][0]["archived"])
		(row,) = [r for r in api.get_conversations() if r["contact"] == contact]
		self.assertTrue(row["archived"])
