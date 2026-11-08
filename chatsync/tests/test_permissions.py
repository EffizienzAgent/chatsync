import frappe
from frappe.tests import IntegrationTestCase

from chatsync.tests.helpers import tag


def make_user(label, roles):
	email = f"{label}-{tag()}@chatsync.test"
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": label, "send_welcome_email": 0}
	).insert(ignore_permissions=True)
	user.add_roles(*roles)
	return user.name


class TestPermissions(IntegrationTestCase):
	def setUp(self):
		self.user_a = make_user("a", ["Chat User"])
		self.user_b = make_user("b", ["System Manager", "Chat User"])
		self.account = frappe.get_doc(
			{
				"doctype": "Chat Account",
				"user": self.user_a,
				"base_url": "http://beeper.test",
				"access_token": "secret",
			}
		).insert(ignore_permissions=True)
		self.chat_a = frappe.get_doc(
			{"doctype": "Chat", "account": self.account.name, "beeper_chat_id": f"!{tag()}:beeper.local"}
		).insert(ignore_permissions=True)
		self.message = frappe.get_doc(
			{
				"doctype": "Chat Message",
				"chat": self.chat_a.name,
				"beeper_message_id": tag(),
				"sent_at": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user("Administrator")
		self.message.delete(ignore_permissions=True)
		self.chat_a.delete(ignore_permissions=True)
		self.account.delete(ignore_permissions=True)
		for user in (self.user_a, self.user_b):
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)

	def test_owner_sees_own_chat(self):
		frappe.set_user(self.user_a)
		self.assertIn(self.chat_a.name, frappe.get_list("Chat", pluck="name"))
		self.assertIn(self.message.name, frappe.get_list("Chat Message", pluck="name"))
		self.assertTrue(frappe.has_permission("Chat", doc=self.chat_a, user=self.user_a))

	def test_other_user_and_system_manager_do_not_see(self):
		frappe.set_user(self.user_b)
		self.assertEqual(frappe.get_list("Chat", pluck="name"), [])
		self.assertEqual(frappe.get_list("Chat Message", pluck="name"), [])
		self.assertFalse(frappe.has_permission("Chat", doc=self.chat_a, user=self.user_b))
		self.assertFalse(frappe.has_permission("Chat Message", doc=self.message, user=self.user_b))
