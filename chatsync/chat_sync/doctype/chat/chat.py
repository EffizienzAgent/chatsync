from frappe.model.document import Document


class Chat(Document):
	def validate(self):
		seen = set()
		for row in list(self.get("parties") or []):
			if (row.party_type, row.party) in seen:
				self.remove(row)
			seen.add((row.party_type, row.party))
