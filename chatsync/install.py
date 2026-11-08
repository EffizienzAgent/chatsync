import frappe

CHAT_INDEX = "unique_account_beeper_chat"
MESSAGE_INDEX = "unique_chat_beeper_message"
MESSAGE_TIME_INDEX = "chat_sent_at"


def after_migrate():
	ensure_role()
	ensure_indexes()


def ensure_role():
	if not frappe.db.exists("Role", "Chat User"):
		frappe.get_doc({"doctype": "Role", "role_name": "Chat User", "desk_access": 1}).insert(
			ignore_permissions=True
		)


def ensure_indexes():
	ensure_unique("Chat", ("account", "beeper_chat_id"), CHAT_INDEX)
	ensure_unique("Chat Message", ("chat", "beeper_message_id"), MESSAGE_INDEX)
	if set(("chat", "sent_at")) <= set(frappe.db.get_table_columns("Chat Message")):
		frappe.db.add_index("Chat Message", ["chat", "sent_at"], MESSAGE_TIME_INDEX)


def ensure_unique(doctype, fields, name):
	if set(fields) <= set(frappe.db.get_table_columns(doctype)):
		frappe.db.add_unique(doctype, list(fields), name)
