from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe

from chatsync import sync
from chatsync.beeper import BeeperError
from chatsync.messages import upsert
from chatsync.tests.helpers import ChatsyncTestCase, tag, use_hooks
from chatsync.tests.test_matching import make_contact

USER = "Administrator"


def ago(days=0, hours=0):
	moment = datetime.now(UTC) - timedelta(days=days, hours=hours)
	return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def message(chat_id, days, text="hi", **extra):
	return {
		"id": f"m{tag()}",
		"chatID": chat_id,
		"senderName": "Them",
		"isSender": False,
		"timestamp": ago(days),
		"sortKey": str(int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)),
		"type": "TEXT",
		"text": text,
		**extra,
	}


def chat_json(phone=None, kind="single", last_days=0, others=1):
	chat_id = f"!{tag()}:beeper.local"
	people = [{"id": "@me:beeper.local", "isSelf": True}]
	for i in range(others):
		person = {"id": f"@p{i}-{tag()}:beeper.local", "fullName": "Other"}
		if phone and i == 0:
			person["phoneNumber"] = phone
		people.append(person)
	return {
		"id": chat_id,
		"accountID": "whatsapp",
		"type": kind,
		"title": "Other",
		"lastActivity": ago(last_days),
		"participants": {"items": people},
	}


def qualifies_none(contact):
	return False


class FakeBeeper:
	def __init__(self, chats, messages):
		self.chat_items = chats
		self.pages = messages
		self.fail_on = None
		self.calls = []

	def chats(self, cursor=None):
		return self.chat_items, None

	def messages(self, chat_id, cursor=None, direction="before"):
		page = int(cursor or 0)
		self.calls.append((chat_id, page))
		if self.fail_on == (chat_id, page):
			raise BeeperError(500, "boom")
		pages = self.pages.get(chat_id, [[]])
		return pages[page], str(page + 1) if page + 1 < len(pages) else None


class TestSync(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		self.number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		self.contact = make_contact(phones=[self.number])

	def run_with(self, fake, function=sync.sync_account, *args):
		with patch.object(sync, "BeeperClient", lambda *a, **k: fake):
			function(*args or (self.account.name,))

	def chats(self):
		return frappe.get_all("Chat", filters={"account": self.account.name}, fields=["*"])

	def texts(self, chat):
		return frappe.get_all("Chat Message", filters={"chat": chat}, pluck="text", order_by="sent_at")

	def test_initial_sync_takes_14_days_of_matched_chats_only(self):
		matched = chat_json(self.number)
		unmatched = chat_json(f"+4916{int(tag()[:7], 16) % 10**8:08d}")
		group = chat_json(self.number, kind="group", others=2)
		fake = FakeBeeper(
			[matched, unmatched, group],
			{
				matched["id"]: [[message(matched["id"], 3, "recent"), message(matched["id"], 20, "old")]],
				unmatched["id"]: [[message(unmatched["id"], 1)]],
				group["id"]: [[message(group["id"], 1)]],
			},
		)
		self.run_with(fake)
		chats = self.chats()
		self.assertEqual([c.beeper_chat_id for c in chats], [matched["id"]])
		self.assertEqual(chats[0].contact, self.contact)
		self.assertEqual(self.texts(chats[0].name), ["recent"])
		account = frappe.get_doc("Chat Account", self.account.name)
		self.assertTrue(account.initial_sync_done)
		self.assertTrue(account.last_synced_at)
		self.assertEqual([c[0] for c in fake.calls], [matched["id"]])

	def test_stored_chat_survives_second_contact_with_same_number(self):
		chat = chat_json(self.number)
		first = message(chat["id"], 1, "first")
		self.run_with(FakeBeeper([chat], {chat["id"]: [[first]]}))
		make_contact(phones=[self.number])
		self.run_with(FakeBeeper([chat], {chat["id"]: [[message(chat["id"], 0, "second"), first]]}))
		(row,) = self.chats()
		self.assertEqual(row.contact, self.contact)
		self.assertEqual(self.texts(row.name), ["first", "second"])

	def test_stored_chat_survives_contact_that_no_longer_qualifies(self):
		chat = chat_json(self.number)
		first = message(chat["id"], 1, "first")
		self.run_with(FakeBeeper([chat], {chat["id"]: [[first]]}))
		use_hooks(self, chatsync_contact_filter=["chatsync.tests.test_sync.qualifies_none"])
		self.run_with(FakeBeeper([chat], {chat["id"]: [[message(chat["id"], 0, "second"), first]]}))
		(row,) = self.chats()
		self.assertEqual(row.contact, self.contact)
		self.assertEqual(self.texts(row.name), ["first", "second"])

	def test_identifier_row_moves_stored_chat(self):
		chat = chat_json(self.number)
		self.run_with(FakeBeeper([chat], {chat["id"]: [[message(chat["id"], 1, "first")]]}))
		other = make_contact(identifiers=[{"network": "whatsapp", "beeper_chat_id": chat["id"]}])
		self.run_with(FakeBeeper([chat], {chat["id"]: [[message(chat["id"], 0, "second")]]}))
		self.assertEqual(self.chats()[0].contact, other)

	def test_reactions_notices_hidden_skipped(self):
		chat = chat_json(self.number)
		page = [
			message(chat["id"], 1, "kept"),
			message(chat["id"], 1, "r", type="REACTION"),
			message(chat["id"], 1, "n", type="NOTICE"),
			message(chat["id"], 1, "h", isHidden=True),
		]
		self.run_with(FakeBeeper([chat], {chat["id"]: [page]}))
		self.assertEqual(self.texts(self.chats()[0].name), ["kept"])

	def test_edit_and_delete_window(self):
		chat = chat_json(self.number)
		edited = message(chat["id"], 0.5, "before edit")
		doomed = message(chat["id"], 0.4, "doomed")
		vanished = message(chat["id"], 0.3, "vanished")
		self.run_with(FakeBeeper([chat], {chat["id"]: [[edited, doomed, vanished]]}))
		name = self.chats()[0].name
		self.assertEqual(len(self.texts(name)), 3)

		second = FakeBeeper(
			[chat],
			{chat["id"]: [[{**edited, "text": "after edit"}, {**doomed, "isDeleted": True}]]},
		)
		self.run_with(second)
		self.assertEqual(self.texts(name), ["after edit"])

	def test_failure_keeps_cursor(self):
		chat = chat_json(self.number)
		pages = [
			[message(chat["id"], 2, "a"), message(chat["id"], 3, "b")],
			[message(chat["id"], 4, "c")],
		]
		fake = FakeBeeper([chat], {chat["id"]: pages})
		fake.fail_on = (chat["id"], 1)
		before = frappe.db.count("Error Log")
		self.run_with(fake)
		self.assertGreater(frappe.db.count("Error Log"), before)
		row = self.chats()[0]
		self.assertFalse(row.newest_sort_key)
		account = frappe.get_doc("Chat Account", self.account.name)
		self.assertFalse(account.last_synced_at)
		self.assertIn("boom", account.last_error)

		fake.fail_on = None
		self.run_with(fake)
		self.assertEqual(sorted(self.texts(row.name)), ["a", "b", "c"])
		self.assertTrue(self.chats()[0].newest_sort_key)
		self.assertEqual(len(self.chats()), 1)

	def test_new_phone_triggers_full_history(self):
		number = f"+4917{int(tag()[:7], 16) % 10**8:08d}"
		contact = make_contact()
		chat = chat_json(number)
		fake = FakeBeeper(
			[chat],
			{chat["id"]: [[message(chat["id"], 1, "new")], [message(chat["id"], 40, "ancient")]]},
		)
		with patch.object(sync, "BeeperClient", lambda *a, **k: fake):
			doc = frappe.get_doc("Contact", contact)
			doc.append("phone_nos", {"phone": number})
			doc.save(ignore_permissions=True)
		rows = self.chats()
		self.assertEqual([r.contact for r in rows], [contact])
		self.assertTrue(rows[0].history_complete)
		self.assertEqual(self.texts(rows[0].name), ["ancient", "new"])

	def test_one_failing_chat_does_not_block_others(self):
		bad, good = chat_json(self.number), chat_json(self.number)
		fake = FakeBeeper(
			[bad, good],
			{bad["id"]: [[message(bad["id"], 1)]], good["id"]: [[message(good["id"], 1, "ok")]]},
		)
		fake.fail_on = (bad["id"], 0)
		self.run_with(fake)
		good_chat = frappe.db.get_value("Chat", {"beeper_chat_id": good["id"]})
		self.assertEqual(self.texts(good_chat), ["ok"])
		account = frappe.get_doc("Chat Account", self.account.name)
		self.assertFalse(account.last_synced_at)
		self.assertIn("1 chat(s) failed", account.last_error)

	def test_empty_page_deletes_nothing(self):
		chat = chat_json(self.number)
		self.run_with(FakeBeeper([chat], {chat["id"]: [[message(chat["id"], 0.5, "keep")]]}))
		name = self.chats()[0].name
		self.run_with(FakeBeeper([chat], {chat["id"]: [[]]}))
		self.assertEqual(self.texts(name), ["keep"])

	def test_unchanged_message_is_not_rewritten(self):
		chat = chat_json(self.number)
		raw = message(chat["id"], 0.5, "same")
		fake = FakeBeeper([chat], {chat["id"]: [[raw]]})
		self.run_with(fake)
		name = frappe.get_all("Chat Message", pluck="name", filters={"beeper_message_id": raw["id"]})[0]
		frappe.db.set_value("Chat Message", name, "modified", "2000-01-01 00:00:00", update_modified=False)
		self.run_with(fake)
		self.assertEqual(str(frappe.db.get_value("Chat Message", name, "modified")), "2000-01-01 00:00:00")

	def test_duplicate_insert_falls_back_to_update(self):
		chat = chat_json(self.number)
		raw = message(chat["id"], 0.5, "one")
		self.run_with(FakeBeeper([chat], {chat["id"]: [[raw]]}))
		name = self.chats()[0].name
		real = frappe.db.get_value
		calls = []

		def first_lookup_misses(*args, **kwargs):
			calls.append(args)
			return None if len(calls) == 1 else real(*args, **kwargs)

		with patch.object(frappe.db, "get_value", first_lookup_misses):
			upsert(name, {**raw, "text": "two"})
		self.assertEqual(self.texts(name), ["two"])

	def test_failed_history_import_is_retried_by_next_sync(self):
		chat = chat_json(self.number)
		fake = FakeBeeper(
			[chat],
			{chat["id"]: [[message(chat["id"], 1, "new")], [message(chat["id"], 40, "ancient")]]},
		)
		fake.fail_on = (chat["id"], 1)
		self.run_with(fake, sync.rematch_contact, self.contact)
		self.assertTrue(self.chats()[0].history_requested)
		self.assertFalse(self.chats()[0].history_complete)
		fake.fail_on = None
		self.run_with(fake)
		row = self.chats()[0]
		self.assertTrue(row.history_complete)
		self.assertEqual(self.texts(row.name), ["ancient", "new"])

	def deleted_at_commit(self, chat):
		"""An unassign in another request becomes visible to the job after its next commit."""
		return patch.object(sync, "commit", lambda: frappe.db.delete("Chat", chat))

	def test_chat_deleted_during_history_import_leaves_no_messages(self):
		chat = chat_json(self.number)
		self.run_with(FakeBeeper([chat], {chat["id"]: [[]]}))
		name = self.chats()[0].name
		pages = [
			[message(chat["id"], 1, "a")],
			[message(chat["id"], 40, "b")],
			[message(chat["id"], 80, "c")],
		]
		fake = FakeBeeper([chat], {chat["id"]: pages})
		with self.deleted_at_commit(name):
			self.run_with(fake, sync.import_history, name)
		self.assertFalse(frappe.db.exists("Chat", name))
		self.assertEqual(frappe.db.count("Chat Message", {"chat": name}), 0)
		self.assertEqual([page for _chat, page in fake.calls], [0])

	def test_chat_deleted_during_sync_leaves_no_messages(self):
		chat = chat_json(self.number)
		self.run_with(FakeBeeper([chat], {chat["id"]: [[]]}))
		name = self.chats()[0].name
		with self.deleted_at_commit(name):
			self.run_with(FakeBeeper([chat], {chat["id"]: [[message(chat["id"], 0, "late")]]}))
		self.assertEqual(frappe.db.count("Chat Message", {"chat": name}), 0)

	def test_contact_update_ignores_unrelated_changes(self):
		with patch.object(sync, "enqueue") as enqueue:
			doc = frappe.get_doc("Contact", self.contact)
			doc.last_name = "Changed"
			doc.save(ignore_permissions=True)
			enqueue.assert_not_called()
			doc.append("phone_nos", {"phone": f"+4917{int(tag()[:7], 16) % 10**8:08d}"})
			doc.save(ignore_permissions=True)
			enqueue.assert_called_once()

	def test_carddav_pull_triggers_rematch_but_initial_import_does_not(self):
		self.addCleanup(frappe.flags.pop, "in_carddav_pull", None)
		self.addCleanup(frappe.flags.pop, "in_carddav_import", None)
		doc = frappe.get_doc("Contact", self.contact)
		with patch.object(sync, "enqueue") as enqueue:
			frappe.flags.in_carddav_pull = True
			frappe.flags.in_carddav_import = True
			doc.append("phone_nos", {"phone": f"+4917{int(tag()[:7], 16) % 10**8:08d}"})
			doc.save(ignore_permissions=True)
			enqueue.assert_not_called()
			frappe.flags.in_carddav_import = False
			doc.append("phone_nos", {"phone": f"+4917{int(tag()[:7], 16) % 10**8:08d}"})
			doc.save(ignore_permissions=True)
			enqueue.assert_called_once()

	def test_new_contact_with_phone_enqueues_once(self):
		with patch.object(sync, "enqueue") as enqueue:
			make_contact(phones=[f"+4917{int(tag()[:7], 16) % 10**8:08d}"])
			enqueue.assert_called_once()
