from unittest.mock import patch

import frappe

from chatsync import api, matching, sync
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json


def number(prefix="15"):
	return f"+49{prefix}{int(tag()[:7], 16) % 10**8:08d}"


def linkedin(title):
	chat = chat_json()
	return {**chat, "accountID": "linkedin", "title": title}


def named_contact(first, last=None):
	return (
		frappe.get_doc({"doctype": "Contact", "first_name": first, "last_name": last})
		.insert(ignore_permissions=True)
		.name
	)


class PagedBeeper(AssignmentBeeper):
	def __init__(self, pages, extra=()):
		super().__init__([c for page in pages for c in page] + list(extra), {})
		self.chat_pages = pages
		self.list_calls = []

	def chats(self, cursor=None, limit=None):
		self.list_calls.append((cursor, limit))
		page = int(cursor or 0)
		return self.chat_pages[page], str(page + 1) if page + 1 < len(self.chat_pages) else None


class InboxCase(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)

	def use(self, fake):
		timeouts = []

		def client(*args, **kwargs):
			timeouts.append(kwargs.get("timeout"))
			return fake

		patcher = patch.object(sync, "BeeperClient", client)
		patcher.start()
		self.addCleanup(patcher.stop)
		return timeouts

	def ids(self, result):
		return [item["beeper_chat_id"] for item in result["items"]]


class TestInboxUnassigned(InboxCase):
	def test_lists_unassigned_chats_of_any_age_page_by_page(self):
		matched = chat_json(number())
		make_contact(phones=[matched["participants"]["items"][1]["phoneNumber"]])
		recent = chat_json(number("16"))
		old = chat_json(number("17"), last_days=400)
		group = chat_json(kind="group", others=2)
		fake = PagedBeeper([[matched, recent, group], [old]])
		self.use(fake)
		before = frappe.db.count("Chat")
		first = api.unassigned_page()
		self.assertEqual((self.ids(first), first["next_cursor"]), ([recent["id"]], "1"))
		second = api.unassigned_page(cursor=first["next_cursor"])
		self.assertEqual((self.ids(second), second["next_cursor"]), ([old["id"]], None))
		self.assertEqual(fake.list_calls, [(None, api.INBOX_PAGE_SIZE), ("1", api.INBOX_PAGE_SIZE)])
		self.assertEqual(frappe.db.count("Chat"), before)
		item = first["items"][0]
		self.assertEqual(item["network_label"], "WhatsApp")
		self.assertEqual(
			(item["ignored"], item["suggested_contact"], item["ambiguous"]), (False, None, False)
		)

	def test_merged_chats_are_expanded_with_short_timeout(self):
		member = chat_json(number())
		merged = {**chat_json(), "merge": {"chatIDs": [member["id"]]}}
		timeouts = self.use(PagedBeeper([[merged]], extra=[member]))
		self.assertEqual(self.ids(api.unassigned_page()), [member["id"]])
		self.assertIn(api.UNASSIGNED_TIMEOUT, timeouts)

	def test_merged_expansion_is_capped_per_page(self):
		first, second = chat_json(number()), chat_json(number("16"))
		merged = [{**chat_json(), "merge": {"chatIDs": [m["id"]]}} for m in (first, second)]
		self.use(PagedBeeper([merged], extra=[first, second]))
		with patch.object(api, "MERGED_SEARCH_LIMIT", 1):
			self.assertEqual(self.ids(api.unassigned_page()), [first["id"]])

	def test_query_searches_all_ages_without_cursor(self):
		old = {**chat_json(number(), last_days=900), "title": "Erika Alt"}
		other = {**chat_json(number("16")), "title": "Hans"}
		fake = PagedBeeper([[other]], extra=[old])
		self.use(fake)
		result = api.unassigned_page(query="erika")
		self.assertEqual((self.ids(result), result["next_cursor"]), ([old["id"]], None))
		self.assertEqual(fake.list_calls, [])

	def test_ignored_chats_only_on_request_and_marked(self):
		kept, hidden = chat_json(number()), chat_json(number("16"))
		self.use(PagedBeeper([[kept, hidden]]))
		api.ignore_chat(hidden["id"])
		self.assertEqual(self.ids(api.unassigned_page()), [kept["id"]])
		result = api.unassigned_page(include_ignored="1")
		self.assertEqual(
			[(i["beeper_chat_id"], i["ignored"]) for i in result["items"]],
			[(kept["id"], False), (hidden["id"], True)],
		)

	def test_unignore_brings_chat_back(self):
		chat = chat_json(number())
		self.use(PagedBeeper([[chat]]))
		api.ignore_chat(chat["id"])
		api.unignore_chat(chat["id"])
		api.unignore_chat(chat["id"])
		self.assertEqual(api.ignored_chats(USER), set())
		self.assertEqual([c["beeper_chat_id"] for c in api.find_unassigned_chats(USER)], [chat["id"]])

	def test_unignore_is_per_user(self):
		chat = chat_json(number())
		self.use(PagedBeeper([[chat]]))
		api.ignore_chat(chat["id"])
		other = make_user("unignore", ["Chat User"])
		frappe.get_doc(
			{"doctype": "Chat Account", "user": other, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		frappe.set_user(other)
		api.unignore_chat(chat["id"])
		self.assertEqual(api.ignored_chats(USER), {chat["id"]})

	def test_find_unassigned_can_include_ignored(self):
		chat = chat_json(number())
		self.use(PagedBeeper([[chat]]))
		api.ignore_chat(chat["id"])
		self.assertEqual(api.find_unassigned_chats(USER), [])
		(item,) = api.find_unassigned_chats(USER, include_ignored=True)
		self.assertTrue(item["ignored"])

	def test_needs_session_account(self):
		frappe.db.set_value("Chat Account", self.account.name, "enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			api.unassigned_page()


class TestNameSuggestions(InboxCase):
	def suggestion(self, title):
		self.use(PagedBeeper([[linkedin(title)]]))
		(item,) = api.unassigned_page()["items"]
		return item["suggested_contact"], item["suggested_contact_name"]

	def test_normalizes_names(self):
		self.assertEqual(matching.name_key("  Jürgen  O'Néil-Smith 🚀 "), "jurgen o neil smith")
		self.assertEqual(matching.name_key("STRASSE"), matching.name_key("straße"))

	def test_unique_full_name_is_suggested(self):
		last = f"Müller{tag()}"
		contact = named_contact("Jürgen", last)
		self.assertEqual(self.suggestion(f"Jurgen {last} 🚀"), (contact, f"Jürgen {last}"))

	def test_last_comma_first_is_suggested(self):
		last = f"Mende{tag()}"
		contact = named_contact("Tobias", last)
		self.assertEqual(self.suggestion(f"{last}, Tobias")[0], contact)

	def test_ambiguous_or_single_word_is_not_suggested(self):
		last = f"Doppelt{tag()}"
		named_contact("Anna", last)
		named_contact("Anna", last)
		self.assertEqual(self.suggestion(f"Anna {last}"), (None, None))
		first = f"Solo{tag()}"
		named_contact(first)
		self.assertEqual(self.suggestion(first), (None, None))

	def test_unreadable_contact_is_not_suggested(self):
		last = f"Geheim{tag()}"
		contact = named_contact("Eva", last)
		with patch.object(matching, "readable", lambda u, contacts: [c for c in contacts if c != contact]):
			self.assertEqual(self.suggestion(f"Eva {last}"), (None, None))

	def test_phone_contact_wins_over_name(self):
		phone = number()
		last = f"Phone{tag()}"
		named_contact("Lea", last)
		use_filter = patch("chatsync.matching.contact_filter", lambda: lambda c: False)
		private = make_contact(phones=[phone])
		chat = {**chat_json(phone), "title": f"Lea {last}"}
		self.use(PagedBeeper([[chat]]))
		with use_filter:
			(item,) = api.unassigned_page()["items"]
		self.assertEqual((item["existing_contact"], item["suggested_contact"]), (private, None))

	def test_chat_with_unknown_number_gets_no_name_suggestion(self):
		last = f"Unknown{tag()}"
		named_contact("Ina", last)
		chat = {**chat_json(number()), "title": f"Ina {last}"}
		self.use(PagedBeeper([[chat]]))
		(item,) = api.unassigned_page()["items"]
		self.assertIsNone(item["suggested_contact"])

	def test_heute_items_carry_the_suggestion(self):
		last = f"Heute{tag()}"
		contact = named_contact("Kim", last)
		self.use(PagedBeeper([[linkedin(f"Kim {last}")]]))
		(item,) = api.find_unassigned_chats(USER)
		self.assertEqual(item["suggested_contact"], contact)

	def test_name_index_is_built_once_per_call(self):
		self.use(PagedBeeper([[linkedin("A B"), linkedin("C D"), linkedin("E F")]]))
		with patch.object(api, "name_index", wraps=matching.name_index) as built:
			self.assertEqual(len(api.unassigned_page()["items"]), 3)
		self.assertEqual(built.call_count, 1)

	def test_suggestion_assigns_with_one_call(self):
		last = f"Assign{tag()}"
		contact = named_contact("Ole", last)
		chat = linkedin(f"Ole {last}")
		self.use(PagedBeeper([[chat]]))
		(item,) = api.unassigned_page()["items"]
		api.assign_chat(item["beeper_chat_id"], item["suggested_contact"])
		self.assertEqual(frappe.db.get_value("Chat", {"beeper_chat_id": chat["id"]}, "contact"), contact)
		self.assertEqual(api.unassigned_page()["items"], [])
