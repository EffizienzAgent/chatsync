from unittest.mock import patch

import frappe

from chatsync import api, sync
from chatsync.beeper import BeeperError
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_chat_view import CSRF, fake_request
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json, message

START = "chatsync.api.start_chat"
ACCOUNTS = [
	{
		"accountID": "whatsapp",
		"bridge": {"id": "whatsapp", "type": "whatsapp"},
		"network": "WhatsApp",
		"user": {"id": "me", "phoneNumber": "+4930111", "fullName": "Me"},
		"status": "connected",
	},
	{
		"accountID": "local-signal_ba_x",
		"bridge": {"id": "local-signal", "type": "signal"},
		"network": "Signal",
		"user": {"id": "me2", "fullName": "Me Signal"},
		"status": "connected",
	},
	{
		"accountID": "linkedin",
		"bridge": {"id": "linkedin", "type": "linkedin"},
		"network": "LinkedIn",
		"user": {"id": "me3", "fullName": "Me"},
		"status": "connected",
	},
]


class StartBeeper(AssignmentBeeper):
	def __init__(self, *args):
		super().__init__(*args)
		self.started = []
		self.failing = None

	def accounts(self):
		return ACCOUNTS

	def start_chat(self, account_id, user):
		self.started.append((account_id, user))
		if self.failing:
			raise BeeperError(404, self.failing)
		raw = chat_json(user["phoneNumber"])
		self.chat_items.append(raw)
		return raw


def number():
	return f"+4915{int(tag()[:7], 16) % 10**8:08d}"


class BrowserCase(ChatsyncTestCase):
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

	def request(self, cmd=START, session_csrf=CSRF, **kwargs):
		for patcher in (
			patch.object(frappe.local, "request", fake_request(**kwargs), create=True),
			patch.dict(frappe.session.data, {"csrf_token": session_csrf}),
			patch.dict(frappe.local.form_dict, {"cmd": cmd}),
		):
			patcher.start()
			self.addCleanup(patcher.stop)


class TestStartChat(BrowserCase):
	def setUp(self):
		super().setUp()
		self.fake = self.use(StartBeeper([], {}))
		self.phone = number()
		self.contact = make_contact(phones=[self.phone[:3] + " " + self.phone[3:]])

	def test_options_list_phone_accounts_and_numbers(self):
		options = api.chat_start_options(self.contact)
		self.assertEqual(
			[(a["account_id"], a["network_label"]) for a in options["accounts"]],
			[("whatsapp", "WhatsApp"), ("local-signal_ba_x", "Signal")],
		)
		self.assertEqual(options["phones"], [self.phone])

	def test_refused_without_browser_session(self):
		with patch.object(frappe.local, "request", None, create=True):
			with self.assertRaises(frappe.PermissionError):
				api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertEqual(self.fake.started, [])

	def test_refused_with_wrong_csrf_or_command(self):
		self.request(csrf="forged")
		with self.assertRaises(frappe.PermissionError):
			api.start_chat(self.contact, "whatsapp", self.phone)
		frappe.local.form_dict["cmd"] = "chatsync.api.send_message"
		frappe.local.request.headers.environ["HTTP_X_FRAPPE_CSRF_TOKEN"] = CSRF
		with self.assertRaises(frappe.PermissionError):
			api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertEqual(self.fake.started, [])

	def test_refused_with_token_auth(self):
		self.request(authorization="token a:b")
		with self.assertRaises(frappe.PermissionError):
			api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertEqual(self.fake.started, [])

	def test_contact_must_be_readable(self):
		user = make_user("start", ["Chat User"])
		frappe.get_doc(
			{"doctype": "Chat Account", "user": user, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		self.request()
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertEqual(self.fake.started, [])

	def test_needs_own_account(self):
		frappe.db.delete("Chat Account", {"user": USER})
		self.request()
		with self.assertRaises(frappe.ValidationError):
			api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertEqual(self.fake.started, [])

	def test_phone_must_belong_to_contact(self):
		self.request()
		with self.assertRaises(frappe.ValidationError):
			api.start_chat(self.contact, "whatsapp", number())
		self.assertEqual(self.fake.started, [])

	def test_starts_and_assigns_without_saving_contact(self):
		self.request()
		frappe.db.set_value("Contact", self.contact, "modified", "2000-01-01 00:00:00", update_modified=False)
		doc_class = frappe.get_doc("Contact", self.contact).__class__
		with patch.object(doc_class, "save", side_effect=AssertionError("save")):
			chat = api.start_chat(self.contact, "whatsapp", self.phone)
		first_name = frappe.db.get_value("Contact", self.contact, "first_name")
		self.assertEqual(
			self.fake.started, [("whatsapp", {"phoneNumber": self.phone, "fullName": first_name})]
		)
		values = frappe.db.get_value("Chat", chat, ["contact", "user", "network"], as_dict=True)
		self.assertEqual((values.contact, values.user), (self.contact, USER))
		rows = frappe.get_all(
			"Chat Identifier",
			filters={"parent": self.contact},
			fields=["beeper_chat_id", "excluded", "parentfield"],
		)
		beeper_chat_id = frappe.db.get_value("Chat", chat, "beeper_chat_id")
		self.assertEqual(
			[(r.beeper_chat_id, r.excluded, r.parentfield) for r in rows],
			[(beeper_chat_id, 0, "chat_identifiers")],
		)
		self.assertEqual(str(frappe.db.get_value("Contact", self.contact, "modified")), "2000-01-01 00:00:00")

	def test_existing_chat_of_same_contact_is_reused(self):
		self.request()
		first = api.start_chat(self.contact, "whatsapp", self.phone)
		self.fake.start_chat = lambda account_id, user: self.fake.chat(
			frappe.db.get_value("Chat", first, "beeper_chat_id")
		)
		self.assertEqual(api.start_chat(self.contact, "whatsapp", self.phone), first)

	def test_chat_of_another_contact_is_not_moved(self):
		self.request()
		other = make_contact(phones=[self.phone])
		taken = api.start_chat(other, "whatsapp", self.phone)
		beeper_chat_id = frappe.db.get_value("Chat", taken, "beeper_chat_id")
		self.fake.start_chat = lambda account_id, user: self.fake.chat(beeper_chat_id)
		with self.assertRaises(frappe.ValidationError) as caught:
			api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertIn("another contact", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Chat", taken, "contact"), other)
		rows = frappe.get_all("Chat Identifier", filters={"beeper_chat_id": beeper_chat_id}, pluck="parent")
		self.assertEqual(rows, [other])

	def test_session_refusal_is_neutral(self):
		with patch.object(frappe.local, "request", None, create=True):
			with self.assertRaises(frappe.PermissionError) as caught:
				api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertIn("browser session", str(caught.exception))

	def test_foreign_phone_is_escaped_in_error(self):
		self.request()
		with self.assertRaises(frappe.ValidationError) as caught:
			api.start_chat(self.contact, "whatsapp", "<img src=x>")
		self.assertNotIn("<img", str(caught.exception))

	def test_beeper_error_is_clear(self):
		self.request()
		self.fake.failing = "user not found"
		with self.assertRaises(frappe.ValidationError) as caught:
			api.start_chat(self.contact, "whatsapp", self.phone)
		self.assertIn("user not found", str(caught.exception))
		self.assertFalse(frappe.db.exists("Chat", {"contact": self.contact}))

	def test_start_chat_is_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.start_chat], ["POST"])


class TestPreviewChat(BrowserCase):
	def setUp(self):
		super().setUp()
		self.raw = chat_json(number())
		self.group = chat_json(kind="group", others=2)
		items = [message(self.raw["id"], 0, f"m{i}", sortKey=str(100 + i)) for i in range(5)]
		items[2]["type"] = "IMAGE"
		items[2]["attachments"] = [{"type": "img", "srcURL": "mxc://x/y", "fileName": "a.png"}]
		items[3].update(type="REACTION")
		items[4].update(isSender=True, text="*bold*")
		self.items = items
		self.fake = self.use(
			AssignmentBeeper([self.raw, self.group], {self.raw["id"]: [list(reversed(items))]})
		)

	def counts(self):
		return frappe.db.count("Chat"), frappe.db.count("Chat Message")

	def test_preview_renders_newest_without_storing(self):
		before = self.counts()
		result = api.preview_chat(self.raw["id"], limit=3)
		self.assertEqual(self.counts(), before)
		rows = result["messages"]
		self.assertEqual([m["text"] for m in rows], ["m1", "m2", "*bold*"])
		self.assertEqual(rows[-1]["html"], "<strong>bold</strong>")
		self.assertEqual(rows[-1]["direction"], "Out")
		self.assertEqual(rows[1]["kind"], "Image")
		self.assertEqual(rows[1]["attachments"][0]["file_name"], "a.png")
		self.assertNotIn("src_url", rows[1]["attachments"][0])
		self.assertEqual(result["title"], self.raw["title"])
		self.assertEqual(result["network_label"], "WhatsApp")

	def test_group_chat_is_previewed(self):
		preview = api.preview_chat(self.group["id"])
		self.assertTrue(preview["is_group"])
		self.assertEqual(preview["network"], "whatsapp")

	def test_assigned_chat_refused(self):
		api.assign_chat(self.raw["id"], make_contact())
		with self.assertRaises(frappe.ValidationError):
			api.preview_chat(self.raw["id"])

	def test_needs_own_account(self):
		user = make_user("preview", ["Chat User"])
		frappe.set_user(user)
		with self.assertRaises(frappe.ValidationError):
			api.preview_chat(self.raw["id"])
		self.assertEqual(self.fake.calls, [])

	def test_preview_is_get(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.preview_chat], ["GET"])
