from unittest.mock import patch

import frappe

from chatsync import api, merges, sync
from chatsync.beeper import BeeperAuthError, BeeperError
from chatsync.matching import counterpart
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_start_preview import BrowserCase
from chatsync.tests.test_sync import USER, ago, chat_json, message


def member(network, phone=None, last_days=0):
	return {**chat_json(phone, last_days=last_days), "accountID": f"{network}.x", "title": "Merged Person"}


def merged(*members, last_days=0):
	return {
		"id": f"!{tag()}:beeper.com",
		"accountID": "matrix",
		"network": "Beeper (Matrix)",
		"type": "single",
		"title": "Merged Person",
		"lastActivity": ago(last_days),
		"participants": {"items": [{"id": "@me:beeper.com", "isSelf": True}, {"id": "@x:beeper.com"}]},
		"merge": {"chatIDs": [m["id"] for m in members]},
	}


class MergeBeeper(AssignmentBeeper):
	def __init__(self, chats, members, messages=None):
		super().__init__(chats, messages or {})
		self.members = {m["id"]: m for m in members}
		self.failing = set()
		self.fetched = []

	def chat(self, chat_id):
		self.fetched.append(chat_id)
		if chat_id in self.failing:
			raise BeeperError(500, "member boom")
		if chat_id in self.members:
			return self.members[chat_id]
		return super().chat(chat_id)


class TestMerges(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		self.number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		self.contact = make_contact(phones=[self.number])
		self.signal = member("signal", self.number)
		self.telegram = member("telegram")
		self.parent = merged(self.signal, self.telegram)

	def use(self, chats, members, messages=None):
		fake = MergeBeeper(chats, members, messages)
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		return fake

	def stored(self):
		return frappe.get_all(
			"Chat", filters={"account": self.account.name}, fields=["beeper_chat_id", "network", "contact"]
		)

	def test_member_ids_from_schema_and_loose_values(self):
		self.assertEqual(merges.member_ids(self.parent), [self.signal["id"], self.telegram["id"]])
		loose = {"merge": [{"chat": "!a:beeper.com"}, "!b:beeper.com", "!a:beeper.com", "x"]}
		self.assertEqual(merges.member_ids(loose), ["!a:beeper.com", "!b:beeper.com"])
		self.assertEqual(merges.expand(None, self.signal), [self.signal])

	def test_merged_parent_has_no_counterpart(self):
		self.assertIsNone(counterpart(self.parent))

	def test_sync_stores_matched_member_only(self):
		fake = self.use(
			[self.parent],
			[self.signal, self.telegram],
			{self.signal["id"]: [[message(self.signal["id"], 1, "via signal")]]},
		)
		sync.sync_account(self.account.name)
		self.assertEqual(
			[(c.beeper_chat_id, c.network, c.contact) for c in self.stored()],
			[(self.signal["id"], "signal", self.contact)],
		)
		(chat,) = frappe.get_all("Chat", filters={"account": self.account.name}, pluck="name")
		self.assertEqual(frappe.get_all("Chat Message", filters={"chat": chat}, pluck="text"), ["via signal"])
		self.assertEqual(sorted(fake.fetched), sorted([self.signal["id"], self.telegram["id"]]))

	def test_member_fetch_error_keeps_other_members_but_not_the_sync_mark(self):
		fake = self.use([self.parent], [self.signal, self.telegram])
		fake.failing = {self.telegram["id"]}
		sync.sync_account(self.account.name)
		self.assertEqual([c.beeper_chat_id for c in self.stored()], [self.signal["id"]])
		account = frappe.get_doc("Chat Account", self.account.name)
		self.assertFalse(account.last_synced_at)
		self.assertEqual(account.last_error, "1 chat(s) failed, last: Beeper API 500: member boom")
		self.assertEqual([r["beeper_chat_id"] for r in api.find_unassigned_chats(USER)], [])

	def test_member_auth_error_fails_the_account(self):
		class AuthDown(MergeBeeper):
			def chat(self, chat_id):
				raise BeeperAuthError(401, "expired")

		fake = AuthDown([self.parent], [self.signal, self.telegram])
		with patch.object(sync, "BeeperClient", lambda *a, **k: fake):
			sync.sync_account(self.account.name)
		account = frappe.get_doc("Chat Account", self.account.name)
		self.assertFalse(account.last_synced_at)
		self.assertIn("401", account.last_error)

	def test_member_shared_and_listed_appears_once(self):
		other = merged(self.signal)
		listed = {**self.signal, "mergedIntoChatID": self.parent["id"]}
		fake = self.use(
			[self.parent, other, listed],
			[self.signal, self.telegram],
			{self.signal["id"]: [[message(self.signal["id"], 1, "via signal")]]},
		)
		sync.sync_account(self.account.name)
		self.assertEqual([c[0] for c in fake.calls], [self.signal["id"]])
		self.assertEqual(len(self.stored()), 1)
		self.contact_numbers_cleared()
		self.assertEqual(
			sorted(r["beeper_chat_id"] for r in api.find_unassigned_chats(USER)),
			sorted([self.signal["id"], self.telegram["id"]]),
		)

	def contact_numbers_cleared(self):
		frappe.db.delete("Chat", {"account": self.account.name})
		frappe.db.delete("Contact Phone", {"parent": self.contact})

	def test_search_expands_at_most_the_cap_with_short_timeout(self):
		parents = [merged(member("telegram")) for _ in range(api.MERGED_SEARCH_LIMIT + 2)]
		fake = MergeBeeper(parents, [])
		for parent in parents:
			for chat_id in parent["merge"]["chatIDs"]:
				fake.members[chat_id] = {**member("telegram"), "id": chat_id}
		timeouts = []

		def client(*args, **kwargs):
			timeouts.append(kwargs.get("timeout"))
			return fake

		with patch.object(sync, "BeeperClient", client):
			result = api.search_chats("merged")
		self.assertEqual(len(result), api.MERGED_SEARCH_LIMIT)
		self.assertEqual(len(fake.fetched), api.MERGED_SEARCH_LIMIT)
		self.assertIn(api.UNASSIGNED_TIMEOUT, timeouts)

	def test_threshold_uses_member_activity(self):
		self.signal["lastActivity"] = ago(20)
		self.use([self.parent], [self.signal, self.telegram])
		sync.sync_account(self.account.name)
		self.assertEqual(self.stored(), [])
		self.assertEqual(
			[r["beeper_chat_id"] for r in api.find_unassigned_chats(USER)], [self.telegram["id"]]
		)

	def test_old_merged_parent_is_not_expanded(self):
		old = merged(member("signal", self.number, last_days=30), last_days=30)
		fake = self.use([self.parent, old], [self.signal, self.telegram])
		sync.sync_account(self.account.name)
		self.assertEqual(sorted(fake.fetched), sorted([self.signal["id"], self.telegram["id"]]))

	def test_members_are_fetched_once_per_run(self):
		twice = merged(self.signal)
		fake = self.use([self.parent, twice], [self.signal, self.telegram])
		sync.sync_account(self.account.name)
		self.assertEqual(fake.fetched.count(self.signal["id"]), 1)

	def test_unassigned_lists_unmatched_member_with_its_network(self):
		self.use([self.parent], [self.signal, self.telegram])
		result = api.find_unassigned_chats(USER)
		self.assertEqual(
			[(r["beeper_chat_id"], r["network"]) for r in result], [(self.telegram["id"], "telegram")]
		)

	def test_search_returns_members(self):
		self.use([self.parent], [self.signal, self.telegram])
		result = api.search_chats("merged")
		self.assertEqual(
			[(r["beeper_chat_id"], r["network"]) for r in result],
			[(self.signal["id"], "signal"), (self.telegram["id"], "telegram")],
		)

	def test_member_is_assigned_and_previewed_by_its_own_id(self):
		self.use(
			[self.parent],
			[self.signal, self.telegram],
			{self.telegram["id"]: [[message(self.telegram["id"], 1, "via telegram")]]},
		)
		preview = api.preview_chat(self.telegram["id"])
		self.assertEqual((preview["network"], len(preview["messages"])), ("telegram", 1))
		api.assign_chat(self.telegram["id"], self.contact)
		self.assertEqual(
			[(c.beeper_chat_id, c.network) for c in self.stored()], [(self.telegram["id"], "telegram")]
		)

	def test_merged_parent_id_is_refused_and_never_stored(self):
		self.use([self.parent], [self.signal, self.telegram])
		with self.assertRaises(frappe.ValidationError):
			api.assign_chat(self.parent["id"], self.contact)
		with self.assertRaises(frappe.ValidationError):
			api.preview_chat(self.parent["id"])
		self.assertEqual(self.stored(), [])

	def test_rematch_stores_matched_member(self):
		self.use([self.parent], [self.signal, self.telegram])
		sync.rematch_contact(self.contact)
		self.assertEqual([c.beeper_chat_id for c in self.stored()], [self.signal["id"]])


class TestStartMerged(BrowserCase):
	def test_start_chat_resolves_merged_result_to_requested_account(self):
		phone = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		contact = make_contact(phones=[phone])
		signal, telegram = member("signal", phone), member("telegram")
		parent = merged(telegram, signal)

		class StartsMerged(MergeBeeper):
			def start_chat(self, account_id, user):
				return parent

		self.use(StartsMerged([], [signal, telegram]))
		self.request()
		chat = api.start_chat(contact, signal["accountID"], phone)
		self.assertEqual(
			frappe.db.get_value("Chat", chat, ["beeper_chat_id", "network"]), (signal["id"], "signal")
		)

	def test_start_chat_without_fitting_member_is_refused(self):
		phone = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		contact = make_contact(phones=[phone])
		telegram = member("telegram")
		parent = merged(telegram)

		class StartsMerged(MergeBeeper):
			def start_chat(self, account_id, user):
				return parent

		self.use(StartsMerged([], [telegram]))
		self.request()
		with self.assertRaises(frappe.ValidationError):
			api.start_chat(contact, "whatsapp", phone)
		self.assertFalse(frappe.db.exists("Chat", {"beeper_chat_id": parent["id"]}))
