import frappe
from frappe.tests import IntegrationTestCase


class TestFixtures(IntegrationTestCase):
	def test_contact_fields_have_english_labels(self):
		labels = frappe.get_all(
			"Custom Field", filters={"dt": "Contact", "module": "Chat Sync"}, fields=["fieldname", "label"]
		)
		self.assertEqual(
			{row.fieldname: row.label for row in labels},
			{"chat_section": "Chat Identifiers", "chat_identifiers": "Chat Identifiers"},
		)
