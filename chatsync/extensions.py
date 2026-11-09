from collections.abc import Callable

import frappe


def contact_filter() -> Callable[[str], bool]:
	"""The `qualifies(contact)` function named by the last app that sets `chatsync_contact_filter`."""
	paths = frappe.get_hooks("chatsync_contact_filter")
	return frappe.get_attr(paths[-1]) if paths else lambda contact: True


def group_parties(contacts: list[str]) -> list[tuple[str, str]]:
	"""The parties (party_type, party) the last app setting `chatsync_group_parties` links to `contacts`."""
	paths = frappe.get_hooks("chatsync_group_parties")
	if not paths or not contacts:
		return []
	found = frappe.get_attr(paths[-1])(list(contacts)) or []
	pairs = (tuple(f[k] for k in ("party_type", "party")) if isinstance(f, dict) else f for f in found)
	return list(dict.fromkeys((str(t), str(p)) for t, p in pairs))


def group_party_type() -> str | None:
	"""The party type a UI offers first when a group has no suggestion: `chatsync_group_party_type`."""
	found = frappe.get_hooks("chatsync_group_party_type")
	return found[-1] if found else None
