from unittest.mock import patch

import frappe

from chatsync import api, sync
from chatsync.tests.helpers import ChatsyncTestCase, tag
from chatsync.tests.test_assignment import AssignmentBeeper
from chatsync.tests.test_matching import make_contact
from chatsync.tests.test_sync import USER, chat_json, message


class TestStaleForm(ChatsyncTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		frappe.db.delete("Chat Account", {"user": USER})
		self.account = frappe.get_doc(
			{"doctype": "Chat Account", "user": USER, "base_url": "http://beeper.test", "access_token": "x"}
		).insert(ignore_permissions=True)
		self.number = f"+4915{int(tag()[:7], 16) % 10**8:08d}"
		self.chat = chat_json(self.number)
		fake = AssignmentBeeper([self.chat], {self.chat["id"]: [[message(self.chat["id"], 1)]]})
		patcher = patch.object(sync, "BeeperClient", lambda *a, **k: fake)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_stale_form_cannot_drop_excluded_row(self):
		contact = make_contact(phones=[self.number])
		stale = frappe.get_doc("Contact", contact)
		name = api.assign_chat(self.chat["id"], contact)
		api.unassign_chat(name)
		stale.chat_identifiers = []
		stale.first_name = "Renamed"
		with patch.object(sync, "enqueue") as enqueue:
			stale.save(ignore_permissions=True)
		enqueue.assert_not_called()
		rows = frappe.get_doc("Contact", contact).chat_identifiers
		self.assertEqual([(r.beeper_chat_id, bool(r.excluded)) for r in rows], [(self.chat["id"], True)])
		sync.sync_account(self.account.name)
		self.assertEqual(frappe.get_all("Chat", filters={"account": self.account.name}), [])

	def test_client_cannot_add_identifier_rows(self):
		contact = make_contact(phones=[self.number])
		doc = frappe.get_doc("Contact", contact)
		doc.append("chat_identifiers", {"network": "whatsapp", "beeper_chat_id": "!x:beeper.local"})
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.get_doc("Contact", contact).chat_identifiers, [])

	def test_assignment_rows_survive_normal_save(self):
		contact = make_contact(phones=[self.number])
		api.assign_chat(self.chat["id"], contact)
		doc = frappe.get_doc("Contact", contact)
		doc.first_name = "Renamed"
		doc.save(ignore_permissions=True)
		ids = [r.beeper_chat_id for r in frappe.get_doc("Contact", contact).chat_identifiers]
		self.assertEqual(ids, [self.chat["id"]])
