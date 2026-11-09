import frappe

from chatsync.extensions import group_party_type


def extend_bootinfo(bootinfo) -> None:
	bootinfo.chatsync_can_chat = "Chat User" in frappe.get_roles() and bool(
		frappe.db.exists("Chat Account", {"user": frappe.session.user, "enabled": 1})
	)
	bootinfo.chatsync_group_party_type = group_party_type()
