from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from chatsync import api, sync
from chatsync.messages import upsert
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_permissions import make_user
from chatsync.tests.test_sync import USER, chat_json, message


class SendingBeeper(AssignmentBeeper):
	def __init__(self, *args):
		super().__init__(*args)
		self.sent = []
		self.latest = []

	def send_text(self, chat_id, text):
		self.sent.append((chat_id, text))
		return {"chatID": chat_id, "pendingMessageID": "pending-1"}

	def messages(self, chat_id, cursor=None, direction="before"):
		if self.sent or self.latest:
			return self.latest, None
		return super().messages(chat_id, cursor, direction)


CSRF = "csrf-token-1"


def fake_request(method="POST", authorization=None, csrf=CSRF):
	headers = {"Authorization": authorization} if authorization else {}
	if csrf:
		headers["X-Frappe-CSRF-Token"] = csrf
	environ = EnvironBuilder(method=method, path="/api/method/chatsync.api.send_message", headers=headers)
	return Request(environ.get_environ())


class TestChatView(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		self.raw = chat_json(number)
		self.fake = SendingBeeper([self.raw], {})
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.chat = api.assign_chat(self.raw["id"], make_contact(phones=[number]))
		frappe.db.delete("Chat Message", {"chat": self.chat})

	def request(self, cmd="chatsync.api.send_message", session_csrf=CSRF, **kwargs):
		patcher = patch.object(frappe.local, "request", fake_request(**kwargs), create=True)
		patcher.start()
		self.addCleanup(patcher.stop)
		session = patch.dict(frappe.session.data, {"csrf_token": session_csrf})
		session.start()
		self.addCleanup(session.stop)
		form = patch.dict(frappe.local.form_dict, {"cmd": cmd})
		form.start()
		self.addCleanup(form.stop)

	def fill(self, count):
		for i in range(count):
			upsert(
				self.raw["id"] and self.chat, message(self.raw["id"], 0, f"m{i:02d}", sortKey=str(1000 + i))
			)
		for i, name in enumerate(
			frappe.get_all("Chat Message", filters={"chat": self.chat}, pluck="name", order_by="creation")
		):
			frappe.db.set_value("Chat Message", name, "sent_at", datetime(2026, 1, 1) + timedelta(minutes=i))

	def test_get_messages_owner_only(self):
		self.fill(2)
		self.assertEqual([m["text"] for m in api.get_messages(self.chat)["messages"]], ["m00", "m01"])
		other = make_user("other", ["Chat User"])
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			api.get_messages(self.chat)

	def test_get_messages_renders_safe_html(self):
		upsert(self.chat, message(self.raw["id"], 0, "<p>Hi Max!</p><script>x()</script><p>LG!</p>"))
		upsert(self.chat, message(self.raw["id"], 0, "*fett* & snake_case"))
		rows = sorted(api.get_messages(self.chat)["messages"], key=lambda m: m["text"])
		self.assertEqual(rows[0]["html"], "<strong>fett</strong> &amp; snake_case")
		self.assertEqual(rows[0]["text"], "*fett* & snake_case")
		self.assertEqual(rows[1]["html"], "<p>Hi Max!</p><p>LG!</p>")

	def test_get_messages_paging(self):
		self.fill(5)
		first = api.get_messages(self.chat, limit=2)
		self.assertEqual([m["text"] for m in first["messages"]], ["m03", "m04"])
		self.assertTrue(first["has_more"])
		second = api.get_messages(self.chat, before=first["messages"][0]["name"], limit=2)
		self.assertEqual([m["text"] for m in second["messages"]], ["m01", "m02"])
		last = api.get_messages(self.chat, before=second["messages"][0]["name"], limit=2)
		self.assertEqual([m["text"] for m in last["messages"]], ["m00"])
		self.assertFalse(last["has_more"])
		self.assertEqual(first["chat"]["network"], frappe.db.get_value("Chat", self.chat, "network"))

	def test_send_rejects_token_auth(self):
		self.request(authorization="token a:b")
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hello")
		self.assertEqual(self.fake.sent, [])

	def test_send_rejects_bearer_auth(self):
		self.request(authorization="Bearer abc123")
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hello")
		self.assertEqual(self.fake.sent, [])

	def test_send_rejects_in_process_call_during_other_request(self):
		self.request(cmd="chatsync.api.search_chats")
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hello")
		self.assertEqual(self.fake.sent, [])

	def test_refresh_stores_confirmed_message(self):
		self.fake.latest = [message(self.raw["id"], 0, "late", isSender=True)]
		result = api.refresh_messages(self.chat)
		self.assertEqual([m["text"] for m in result["messages"]], ["late"])
		self.assertEqual(self.fake.sent, [])

	def test_refresh_is_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[api.refresh_messages], ["POST"])

	def test_send_rejects_session_without_csrf_token(self):
		self.request(session_csrf=None)
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hello")
		self.assertEqual(self.fake.sent, [])

	def test_send_rejects_wrong_csrf_header(self):
		self.request(csrf="forged")
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hello")
		self.assertEqual(self.fake.sent, [])

	def test_send_rejects_without_request(self):
		with patch.object(frappe.local, "request", None, create=True):
			with self.assertRaises(frappe.PermissionError):
				api.send_message(self.chat, "hello")
		self.assertEqual(self.fake.sent, [])

	def test_send_rejects_non_post(self):
		self.request(method="GET")
		with self.assertRaises(frappe.PermissionError):
			api.send_message(self.chat, "hello")

	def test_send_from_session_stores_message(self):
		self.request()
		sent = message(self.raw["id"], 0, "hello", isSender=True, sendStatus={"status": "SUCCESS"})
		pending = message(self.raw["id"], 0, "later", isSender=True, sendStatus={"status": "PENDING"})
		self.fake.latest = [pending, sent]
		result = api.send_message(self.chat, "  hello ")
		self.assertEqual(self.fake.sent, [(self.raw["id"], "hello")])
		self.assertEqual([(m["text"], m["direction"]) for m in result["messages"]], [("hello", "Out")])
		self.assertEqual(frappe.get_all("Chat Message", filters={"chat": self.chat}, pluck="text"), ["hello"])
		api.send_message(self.chat, "again")
		self.assertEqual(frappe.db.count("Chat Message", {"chat": self.chat}), 1)

	def test_send_rejects_empty_text(self):
		self.request()
		with self.assertRaises(frappe.ValidationError):
			api.send_message(self.chat, "   ")
		self.assertEqual(self.fake.sent, [])

	def test_reassign_returns_new_chat_name(self):
		other = make_contact()
		new = api.reassign_chat(self.chat, other)
		self.assertNotEqual(new, self.chat)
		self.assertEqual(frappe.db.get_value("Chat", new, "contact"), other)
		self.assertFalse(frappe.db.exists("Chat", self.chat))
