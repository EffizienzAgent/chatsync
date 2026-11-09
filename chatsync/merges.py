"""Beeper merged chats group one person's chats across networks into a single list entry.

A merged chat holds no messages of its own; chatsync works with its member chats only.
"""

import frappe

from chatsync.beeper import BeeperAuthError, BeeperError

LOGGED_FOR_SECONDS = 24 * 60 * 60


def is_merged(raw: dict) -> bool:
	return raw.get("merge") is not None


def member_ids(raw: dict) -> list[str]:
	merge = raw.get("merge")
	ids = merge.get("chatIDs") if isinstance(merge, dict) else None
	if not isinstance(ids, list):
		ids = list(chat_ids_in(merge))
	return list(dict.fromkeys(i for i in ids if isinstance(i, str) and i.startswith("!")))


def chat_ids_in(value):
	if isinstance(value, str):
		yield value
	elif isinstance(value, dict):
		for item in value.values():
			yield from chat_ids_in(item)
	elif isinstance(value, list):
		for item in value:
			yield from chat_ids_in(item)


def expand(client, raw: dict, cache: dict | None = None) -> list[dict]:
	"""The member chats of a merged chat (fetched once per `cache`), else the chat itself."""
	if not is_merged(raw):
		return [raw]
	cache = {} if cache is None else cache
	members = []
	for chat_id in member_ids(raw):
		if chat_id not in cache:
			cache[chat_id] = fetch(client, chat_id)
		found = cache[chat_id]
		if found and not is_merged(found):
			members.append(found)
	return members


def fetch(client, chat_id: str) -> dict | None:
	try:
		return client.chat(chat_id)
	except BeeperAuthError:
		raise
	except BeeperError as e:
		key = f"chatsync-member-failed-{chat_id}"
		if not frappe.cache.get_value(key):
			frappe.cache.set_value(key, 1, expires_in_sec=LOGGED_FOR_SECONDS)
			frappe.log_error(title=f"Chat Sync could not load merged member {chat_id}: {e}")
		return None
