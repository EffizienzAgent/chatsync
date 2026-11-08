import frappe

from chatsync.boot import extend_bootinfo
from chatsync.tests.helpers import ChatsyncTestCase
from chatsync.tests.test_merged_view import make_account
from chatsync.tests.test_permissions import make_user


class TestBoot(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")

	def can_chat(self, user):
		frappe.set_user(user)
		bootinfo = frappe._dict()
		extend_bootinfo(bootinfo)
		return bootinfo.chatsync_can_chat

	def test_needs_role_and_enabled_account(self):
		user = make_user("boot", ["Chat User"])
		self.assertFalse(self.can_chat(user))
		frappe.set_user("Administrator")
		account = make_account(user)
		self.assertTrue(self.can_chat(user))
		frappe.set_user("Administrator")
		frappe.db.set_value("Chat Account", account.name, "enabled", 0)
		self.assertFalse(self.can_chat(user))
		frappe.set_user("Administrator")
		frappe.db.set_value("Chat Account", account.name, "enabled", 1)
		frappe.get_doc("User", user).remove_roles("Chat User")
		self.assertFalse(self.can_chat(user))
