import uuid
from unittest.mock import patch

import frappe


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
