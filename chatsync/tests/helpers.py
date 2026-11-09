import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase


def tag():
	return uuid.uuid4().hex[:10]


def use_hooks(case, **hooks):
	real = frappe.get_hooks

	def get_hooks(hook=None, *args, **kwargs):
		if hook in hooks:
			return list(hooks[hook])
		return real(hook, *args, **kwargs)

	patcher = patch.object(frappe, "get_hooks", get_hooks)
	patcher.start()
	case.addCleanup(patcher.stop)


class ChatsyncTestCase(IntegrationTestCase):
	"""Pins chatsync's host hooks to their defaults so tests behave the same in any host site."""

	def setUp(self):
		use_hooks(self, chatsync_contact_filter=[], chatsync_group_parties=[], chatsync_group_party_type=[])
