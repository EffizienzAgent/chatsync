from frappe.tests import IntegrationTestCase

from chatsync.messages import receipt_of


def sent(**extra):
	return {"isSender": True, "sendStatus": {"status": "SUCCESS"}, **extra}


class TestReceipts(IntegrationTestCase):
	def test_incoming_has_no_receipt(self):
		self.assertIsNone(receipt_of({"isSender": False, "seen": True}))

	def test_sent_delivered_read(self):
		self.assertEqual(receipt_of(sent()), "Sent")
		self.assertEqual(receipt_of({"isSender": True}), "Sent")
		delivered = sent(sendStatus={"status": "SUCCESS", "deliveredToUsers": ["@them"]})
		self.assertEqual(receipt_of(delivered), "Delivered")
		self.assertEqual(receipt_of({**delivered, "seen": True}), "Read")

	def test_seen_shapes(self):
		self.assertEqual(receipt_of(sent(seen="2026-11-18T10:00:00Z")), "Read")
		self.assertEqual(receipt_of(sent(seen={"@them": "2026-11-18T10:00:00Z"})), "Read")
		self.assertEqual(receipt_of(sent(seen={"@them": False})), "Sent")
		self.assertEqual(receipt_of(sent(seen=False)), "Sent")
		self.assertEqual(receipt_of(sent(seen="")), "Sent")

	def test_failed_wins(self):
		failed = {"isSender": True, "seen": True, "sendStatus": {"status": "FAIL_PERMANENT"}}
		self.assertEqual(receipt_of(failed), "Failed")
