import json
from unittest.mock import patch

import frappe

from chatsync import api, sync
from chatsync.tests.helpers import ChatsyncTestCase, tag, use_hooks
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_chat_view import fake_request
from chatsync.tests.test_inbox_unassigned import named_contact, number
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, ago, chat_json, message

ASSIGN = "chatsync.api.assign_group"
SEND = "chatsync.api.send_message"
PARTIES: dict[str, list] = {}
SELF = "@me:beeper.local"


def parties_of(contacts):
	return [party for contact in contacts for party in PARTIES.get(contact, [])]


def person(name="Member", phone=None, **extra):
	found = {"id": f"@p-{tag()}:beeper.local", "fullName": name, **extra}
	if phone:
		found["phoneNumber"] = phone
	return found


def group_json(*people, title="Projekt Alpha", last_days=0, **extra):
	items = [{"id": SELF, "isSelf": True}, *people]
	return {
		"id": f"!{tag()}:beeper.local",
		"accountID": "whatsapp",
		"type": "group",
		"title": title,
		"lastActivity": ago(last_days),
		"participants": {"items": items, "hasMore": False, "total": len(items)},
		**extra,
	}


class GroupBeeper(AssignmentBeeper):
	def __init__(self, chats, messages=None):
		super().__init__(chats, messages or {})
		self.sent = []
		self.full = {}
		self.archived = []

	def chats(self, cursor=None, limit=None):
		return self.chat_items, None

	def chat(self, chat_id, max_participants=None):
		if max_participants == -1 and chat_id in self.full:
			return self.full[chat_id]
		return super().chat(chat_id)

	def send_text(self, chat_id, text):
		self.sent.append((chat_id, text))
		return {"pendingMessageID": "pending-1"}

	def archive(self, chat_id, archived=True):
		self.archived.append((chat_id, archived))


class GroupCase(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(PARTIES.clear)
		use_hooks(
			self,
			chatsync_contact_filter=[],
			chatsync_group_parties=["chatsync.tests.test_groups.parties_of"],
			chatsync_group_party_type=[],
		)
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		self.user_a = make_user("party-a", [])
		self.user_b = make_user("party-b", [])
		self.phone_a, self.phone_b = number(), number("16")
		self.anna = make_contact("anna", phones=[self.phone_a])
		self.ben = make_contact("ben", phones=[self.phone_b])
		PARTIES[self.anna] = [("User", self.user_a)]
		PARTIES[self.ben] = [("User", self.user_a), ("User", self.user_b)]
		self.anna_p = person("Anna", self.phone_a)
		self.ben_p = person("Ben", self.phone_b)
		self.stranger_p = person("Somebody Unknown", number("17"))
		self.raw = group_json(self.anna_p, self.ben_p, self.stranger_p)
		self.history = [[message(self.raw["id"], 1, "hello group", senderID=self.anna_p["id"])]]
		self.fake = self.use(GroupBeeper([self.raw], {self.raw["id"]: self.history}))

	def use(self, fake):
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		return fake

	def browser(self, cmd=ASSIGN, **kwargs):
		"""Each call replaces the previous fake request (frappe.local cannot stack patches)."""
		if not getattr(self, "patchers", None):
			self.patchers = []
			self.addCleanup(self.leave_browser)
		self.leave_browser()
		self.patchers = [
			patch.object(frappe.local, "request", fake_request(**kwargs), create=True),
			patch.dict(frappe.session.data, {"csrf_token": "csrf-token-1"}),
			patch.dict(frappe.local.form_dict, {"cmd": cmd}),
		]
		for patcher in self.patchers:
			patcher.start()

	def leave_browser(self):
		while self.patchers:
			self.patchers.pop().stop()

	def assign(self, parties=None, contacts=None):
		self.browser()
		parties = [{"party_type": "User", "party": self.user_a}] if parties is None else parties
		contacts = [self.anna, self.ben] if contacts is None else contacts
		return api.assign_group(self.raw["id"], json.dumps(parties), json.dumps(contacts))

	def group_items(self, **kwargs):
		return [i for i in api.unassigned_page(**kwargs)["items"] if i.get("is_group")]


class TestGroupSuggestions(GroupCase):
	def test_sync_never_stores_a_group_by_itself(self):
		sync.sync_account(self.account.name)
		self.assertFalse(frappe.db.exists("Chat", {"beeper_chat_id": self.raw["id"]}))
		self.assertEqual(frappe.db.count("Chat Message", {"text": "hello group"}), 0)

	def test_unassigned_lists_group_with_members_and_suggested_parties(self):
		[item] = self.group_items()
		self.assertEqual((item["beeper_chat_id"], item["title"]), (self.raw["id"], "Projekt Alpha"))
		self.assertEqual(item["member_count"], 3)
		self.assertEqual(
			[(m["display_name"], m["contact"]) for m in item["members"]],
			[("Anna", self.anna), ("Ben", self.ben), ("Somebody Unknown", None)],
		)
		self.assertEqual(
			[(p["party_type"], p["party"]) for p in item["suggested_parties"]],
			[("User", self.user_a), ("User", self.user_b)],
		)
		self.assertIsNone(item["suggested_contact"])

	def test_members_match_by_identifier_and_name(self):
		handle = f"h{tag()}"
		by_id = make_contact("carla", identifiers=[{"network": "whatsapp", "identifier": handle}])
		dora = named_contact(f"Dora{tag()}", "Group")
		raw = group_json(
			person("C", username=handle), person(frappe.db.get_value("Contact", dora, "full_name"))
		)
		self.fake.chat_items.append(raw)
		item = next(i for i in self.group_items() if i["beeper_chat_id"] == raw["id"])
		self.assertEqual([m["contact"] for m in item["members"]], [by_id, dora])
		self.assertEqual(item["suggested_parties"], [])

	def test_without_host_hook_nothing_is_suggested(self):
		use_hooks(self, chatsync_group_parties=[])
		[item] = self.group_items()
		self.assertEqual(item["suggested_parties"], [])
		self.assertEqual(item["members"][0]["contact"], self.anna)

	def test_unreadable_member_contact_is_not_matched(self):
		other = make_user("reader", ["Chat User"])
		frappe.db.delete("Chat Account", {"user": USER})
		frappe.get_doc(
			{"doctype": "Chat Account", "user": other, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		frappe.set_user(other)
		[item] = self.group_items()
		self.assertEqual([m["contact"] for m in item["members"]], [None, None, None])

	def test_search_finds_groups(self):
		[item] = self.group_items(query="alpha")
		self.assertEqual(item["beeper_chat_id"], self.raw["id"])

	def test_preview_and_done_work_for_an_unassigned_group(self):
		preview = api.preview_chat(self.raw["id"])
		self.assertTrue(preview["is_group"])
		self.assertEqual([m["text"] for m in preview["messages"]], ["hello group"])
		self.browser("chatsync.api.set_chat_done")
		api.set_chat_done(beeper_chat_id=self.raw["id"])
		self.assertEqual(self.fake.archived, [(self.raw["id"], True)])


class TestAssignGroup(GroupCase):
	def test_assign_stores_parties_members_and_history(self):
		name = self.assign(
			parties=[
				{"party_type": "User", "party": self.user_a},
				{"party_type": "User", "party": self.user_b},
			],
			contacts=[self.anna],
		)
		doc = frappe.get_doc("Chat", name)
		self.assertEqual((doc.is_group, doc.contact, doc.user, doc.title), (1, None, USER, "Projekt Alpha"))
		self.assertEqual(
			[(p.party_type, p.party) for p in doc.parties], [("User", self.user_a), ("User", self.user_b)]
		)
		self.assertEqual(
			[(m.display_name, m.contact, m.phone) for m in doc.members],
			[
				("Anna", self.anna, self.phone_a),
				("Ben", None, self.phone_b),
				("Somebody Unknown", None, self.stranger_p["phoneNumber"]),
			],
		)
		self.assertEqual(doc.self_id, SELF)
		self.assertEqual((doc.history_complete, doc.network), (1, "whatsapp"))
		[stored] = frappe.get_all("Chat Message", filters={"chat": name}, fields=["text", "sender_id"])
		self.assertEqual((stored.text, stored.sender_id), ("hello group", self.anna_p["id"]))
		self.assertFalse(frappe.get_all("Chat Identifier", filters={"parent": ["in", [self.anna, self.ben]]}))
		self.assertEqual(self.group_items(), [])

	def test_assign_fetches_all_participants_of_a_large_group(self):
		extra = person("Late Member")
		self.raw["participants"]["hasMore"] = True
		self.fake.full[self.raw["id"]] = {
			**self.raw,
			"participants": {
				"items": [*self.raw["participants"]["items"], extra],
				"hasMore": False,
				"total": 5,
			},
		}
		doc = frappe.get_doc("Chat", self.assign())
		self.assertIn("Late Member", [m.display_name for m in doc.members])

	def test_assign_needs_browser_session(self):
		with self.assertRaises(frappe.PermissionError):
			api.assign_group(self.raw["id"], json.dumps([{"party_type": "User", "party": self.user_a}]), "[]")
		self.browser(authorization="token a:b")
		with self.assertRaises(frappe.PermissionError):
			api.assign_group(self.raw["id"], json.dumps([{"party_type": "User", "party": self.user_a}]), "[]")
		self.assertFalse(frappe.db.exists("Chat", {"beeper_chat_id": self.raw["id"]}))

	def test_assign_rejects_contacts_that_are_no_members(self):
		with self.assertRaises(frappe.ValidationError):
			self.assign(contacts=[make_contact("outsider")])

	def test_assign_needs_a_party_or_a_contact(self):
		with self.assertRaises(frappe.ValidationError):
			self.assign(parties=[], contacts=[])

	def test_assign_rejects_missing_or_unreadable_parties(self):
		with self.assertRaises(frappe.ValidationError):
			self.assign(parties=[{"party_type": "User", "party": "nobody@example.invalid"}])
		with self.assertRaises(frappe.ValidationError):
			self.assign(parties=[{"party_type": "No Such Type", "party": "x"}])
		reader = make_user("reader", ["Chat User"])
		frappe.db.delete("Chat Account", {"user": USER})
		frappe.get_doc(
			{"doctype": "Chat Account", "user": reader, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		frappe.set_user(reader)
		with self.assertRaises(frappe.PermissionError):
			self.assign(parties=[{"party_type": "User", "party": self.user_a}], contacts=[])

	def test_assign_refuses_direct_chats(self):
		direct = chat_json(number("18"))
		self.fake.chat_items.append(direct)
		self.browser()
		with self.assertRaises(frappe.ValidationError):
			api.assign_group(direct["id"], json.dumps([{"party_type": "User", "party": self.user_a}]), "[]")


class TestStoredGroups(GroupCase):
	def setUp(self):
		super().setUp()
		self.chat = self.assign()

	def test_sync_updates_messages_and_new_members(self):
		self.raw["participants"]["items"] = [
			p for p in self.raw["participants"]["items"] if p["id"] != self.stranger_p["id"]
		]
		phone = number("19")
		carla = make_contact("carla", phones=[phone])
		self.raw["participants"]["items"].append(person("Carla", phone))
		self.raw["participants"]["items"].append(person("Dieter", number("15")))
		reaction = {"reactionKey": "👍", "participantID": SELF}
		self.fake.pages[self.raw["id"]] = [
			[
				message(self.raw["id"], 0, "new one", senderID=self.ben_p["id"], reactions=[reaction]),
				*self.history[0],
			]
		]
		sync.sync_account(self.account.name)
		doc = frappe.get_doc("Chat", self.chat)
		self.assertEqual(
			[(m.display_name, m.contact) for m in doc.members],
			[("Anna", self.anna), ("Ben", self.ben), ("Carla", carla), ("Dieter", None)],
		)
		rows = frappe.get_all(
			"Chat Message", filters={"chat": self.chat}, fields=["text", "reactions"], order_by="sent_at"
		)
		self.assertEqual([r.text for r in rows], ["hello group", "new one"])
		self.assertTrue(json.loads(rows[1].reactions)[0]["is_self"])

	def test_sync_keeps_members_the_user_left_unlinked(self):
		frappe.db.set_value("Chat Member", {"parent": self.chat, "contact": self.ben}, "contact", None)
		sync.sync_account(self.account.name)
		members = frappe.get_doc("Chat", self.chat).members
		self.assertEqual([m.contact for m in members], [self.anna, None, None])

	def test_unassign_deletes_and_excludes_until_assigned_again(self):
		api.unassign_chat(self.chat)
		self.assertFalse(frappe.db.exists("Chat", self.chat))
		self.assertEqual(frappe.db.count("Chat Message", {"chat": self.chat}), 0)
		self.assertEqual(self.group_items(), [])
		[hidden] = self.group_items(include_ignored=1)
		self.assertTrue(hidden["ignored"])
		self.assertIn(self.raw["id"], api.excluded_groups(USER))
		sync.sync_account(self.account.name)
		self.assertFalse(frappe.db.exists("Chat", {"beeper_chat_id": self.raw["id"]}))
		self.assign()
		self.assertNotIn(self.raw["id"], api.excluded_groups(USER))

	def test_get_messages_shows_members_parties_and_senders(self):
		data = api.get_messages(self.chat)
		self.assertTrue(data["chat"]["is_group"])
		self.assertEqual(
			[(m["display_name"], m["contact"]) for m in data["chat"]["members"]][:2],
			[("Anna", self.anna), ("Ben", self.ben)],
		)
		self.assertEqual(data["chat"]["parties"][0]["party"], self.user_a)
		self.assertEqual(data["messages"][0]["sender_id"], self.anna_p["id"])

	def test_owner_only(self):
		other = make_user("other", ["Chat User"])
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			api.get_messages(self.chat)
		self.assertEqual(frappe.get_list("Chat", filters={"name": self.chat}), [])
		self.browser(SEND)
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hi")

	def test_send_into_group_uses_the_same_guards(self):
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hi")
		self.browser(SEND, authorization="Bearer x")
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hi")
		self.assertEqual(self.fake.sent, [])
		self.browser(SEND)
		api.send_message(self.chat, "hi all")
		self.assertEqual(self.fake.sent, [(self.raw["id"], "hi all")])

	def test_contact_view_and_quick_reply_keep_direct_chats_only(self):
		direct = chat_json(self.phone_a)
		self.fake.chat_items.append(direct)
		direct_chat = api.assign_chat(direct["id"], self.anna)
		data = api.get_contact_messages(self.anna)
		self.assertEqual([c["name"] for c in data["chats"]], [direct_chat])

	def test_conversations_and_open_chats_mark_groups(self):
		[row] = [r for r in api.get_conversations() if r["chat"] == self.chat]
		self.assertEqual((row["is_group"], row["title"]), (True, "Projekt Alpha"))
		[open_row] = [r for r in api.open_assigned_chats(USER) if r["chat"] == self.chat]
		self.assertEqual((open_row["is_group"], open_row["contact"]), (True, None))
		self.assertEqual(open_row["contact_name"], "Projekt Alpha")

	def test_reassign_refuses_groups(self):
		with self.assertRaises(frappe.ValidationError):
			api.reassign_chat(self.chat, self.anna)
