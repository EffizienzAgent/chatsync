from collections.abc import Callable

import frappe


def contact_filter() -> Callable[[str], bool]:
	"""The `qualifies(contact)` function named by the last app that sets `chatsync_contact_filter`."""
	paths = frappe.get_hooks("chatsync_contact_filter")
	return frappe.get_attr(paths[-1]) if paths else lambda contact: True
