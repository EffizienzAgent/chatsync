import frappe

FIELDS = {"In": "last_incoming_at", "Out": "last_outgoing_at"}


def execute():
	"""Chats synced before v0.6.0 learn when their last message came in and went out."""
	table = frappe.qb.DocType("Chat Message")
	rows = (
		frappe.qb.from_(table)
		.select(table.chat, table.direction, frappe.qb.functions("MAX", table.sent_at))
		.where(table.direction.isin(list(FIELDS)))
		.groupby(table.chat, table.direction)
		.run()
	)
	values: dict[str, dict] = {}
	for chat, direction, sent_at in rows:
		values.setdefault(chat, {})[FIELDS[direction]] = sent_at
	for chat, fields in values.items():
		frappe.db.set_value("Chat", chat, fields, update_modified=False)
