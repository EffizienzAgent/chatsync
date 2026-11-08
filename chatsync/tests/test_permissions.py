import frappe

from chatsync.tests.helpers import ChatsyncTestCase, tag


def make_user(label, roles):
	email = f"{label}-{tag()}@chatsync.test"
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": label, "send_welcome_email": 0}
	).insert(ignore_permissions=True)
	user.add_roles(*roles)
	return user.name


class TestPermissions(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
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

	def test_create_on_foreign_parent_is_refused(self):
		chat = frappe.new_doc("Chat")
		chat.update({"account": self.account.name, "beeper_chat_id": f"!{tag()}:beeper.local"})
		message = frappe.new_doc("Chat Message")
		message.update({"chat": self.chat_a.name, "beeper_message_id": tag()})
		self.assertFalse(frappe.has_permission("Chat", "create", doc=chat, user=self.user_b))
		self.assertFalse(frappe.has_permission("Chat Message", "create", doc=message, user=self.user_b))
		self.assertTrue(frappe.has_permission("Chat", "read", doc=chat, user=self.user_a))
		self.assertTrue(frappe.has_permission("Chat Message", "read", doc=message, user=self.user_a))

	def test_new_doc_without_parent_is_left_to_validation(self):
		self.assertTrue(frappe.has_permission("Chat", "create", doc=frappe.new_doc("Chat"), user=self.user_b))

	def test_chat_user_cannot_write_chats_or_messages(self):
		frappe.set_user(self.user_a)
		new_chat = {
			"doctype": "Chat",
			"account": self.account.name,
			"beeper_chat_id": f"!{tag()}:beeper.local",
		}
		new_message = {
			"doctype": "Chat Message",
			"chat": self.chat_a.name,
			"beeper_message_id": tag(),
			"sent_at": frappe.utils.now_datetime(),
		}
		for values in (new_chat, new_message):
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(values).insert()
		chat = frappe.get_doc("Chat", self.chat_a.name)
		chat.title = "changed"
		with self.assertRaises(frappe.PermissionError):
			chat.save()
		message = frappe.get_doc("Chat Message", self.message.name)
		message.text = "forged"
		with self.assertRaises(frappe.PermissionError):
			message.save()

	def test_forged_account_on_own_chat_is_refused(self):
		other = make_user("c", ["Chat User"])
		self.addCleanup(frappe.delete_doc, "User", other, force=True, ignore_permissions=True)
		account = frappe.get_doc(
			{"doctype": "Chat Account", "user": other, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		chat = frappe.get_doc(
			{"doctype": "Chat", "account": account.name, "beeper_chat_id": f"!{tag()}:beeper.local"}
		).insert(ignore_permissions=True)
		self.addCleanup(account.delete, ignore_permissions=True)
		self.addCleanup(chat.delete, ignore_permissions=True)
		frappe.set_user(other)
		forged = frappe.get_doc("Chat", chat.name)
		forged.account, forged.user = self.account.name, other
		self.assertFalse(frappe.has_permission("Chat", "read", doc=forged, user=other))
		with self.assertRaises(frappe.PermissionError):
			forged.save()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Chat", chat.name, "account"), account.name)

	def test_only_administrator_bypasses(self):
		self.assertIn(self.chat_a.name, frappe.get_list("Chat", pluck="name", user="Administrator"))
		frappe.set_user(self.user_b)
		frappe.flags.in_job = True
		self.addCleanup(setattr, frappe.flags, "in_job", False)
		self.assertEqual(frappe.get_list("Chat", pluck="name"), [])
		self.assertEqual(frappe.get_list("Chat Message", pluck="name"), [])

	def test_chat_account_visibility(self):
		other = make_user("c", ["Chat User"])
		self.addCleanup(frappe.delete_doc, "User", other, force=True, ignore_permissions=True)
		frappe.set_user(self.user_a)
		self.assertEqual(frappe.get_list("Chat Account", pluck="name"), [self.account.name])
		frappe.set_user(other)
		self.assertEqual(frappe.get_list("Chat Account", pluck="name"), [])
		self.assertFalse(frappe.has_permission("Chat Account", doc=self.account, user=other))
		frappe.set_user(self.user_b)
		self.assertEqual(frappe.get_list("Chat Account", pluck="name"), [self.account.name])
		self.assertTrue(frappe.has_permission("Chat Account", "write", doc=self.account, user=self.user_b))
