import frappe

OWNER_FIELD = {"Chat Account": "user", "Chat": "user"}


def is_unrestricted(user: str) -> bool:
	return user == "Administrator" or bool(frappe.flags.in_job)


def owner_of(doc) -> str | None:
	if doc.doctype == "Chat Message":
		return frappe.db.get_value("Chat", doc.chat, "user")
	return doc.get(OWNER_FIELD[doc.doctype])


def condition(doctype: str, user: str) -> str:
	if doctype == "Chat Message":
		return (
			"exists (select 1 from `tabChat` where `tabChat`.`name` = `tabChat Message`.`chat`"
			f" and `tabChat`.`user` = {frappe.db.escape(user)})"
		)
	return f"`tab{doctype}`.`user` = {frappe.db.escape(user)}"


def conditions_for(doctype: str, user: str | None) -> str:
	user = user or frappe.session.user
	return "" if is_unrestricted(user) else condition(doctype, user)


def chat_account_conditions(user=None) -> str:
	return conditions_for("Chat Account", user)


def query_conditions(user=None) -> str:
	return conditions_for("Chat", user)


def chat_message_conditions(user=None) -> str:
	return conditions_for("Chat Message", user)


def has_permission(doc, ptype=None, user=None, debug=False) -> bool:
	user = user or frappe.session.user
	if is_unrestricted(user) or doc.is_new():
		return True
	return owner_of(doc) == user
