"""Beeper merged chats group one person's chats across networks into a single list entry.

A merged chat holds no messages of its own; chatsync works with its member chats only. Beeper's
inbox lists the merged chat in place of its members, so a member is archived when its merged chat is.
"""

import frappe

from chatsync.beeper import BeeperAuthError, BeeperError


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
	"""The member chats of a merged chat, else the chat itself.

	`cache` maps member IDs to their chat, or to the BeeperError that kept it from loading,
	so each member is fetched once per run and the caller can count failures. It also keeps the
	merged chat, for `archived`. Members carry the merged chat's ID and archive state.
	"""
	if not is_merged(raw):
		return [raw]
	cache = {} if cache is None else cache
	cache.setdefault(raw["id"], raw)
	state = {"mergedIntoChatID": raw["id"], "isArchived": bool(raw.get("isArchived"))}
	members = []
	for chat_id in member_ids(raw):
		if chat_id not in cache:
			cache[chat_id] = fetch(client, chat_id)
		found = cache[chat_id]
		if isinstance(found, dict) and not is_merged(found):
			members.append({**found, **state})
	return members


def archived(client, raw: dict, cache: dict) -> bool:
	"""The chat's archive state as Beeper's inbox shows it: a merged member's is its merged chat's."""
	parent_id = raw.get("mergedIntoChatID")
	if not parent_id:
		return bool(raw.get("isArchived"))
	if parent_id not in cache:
		cache[parent_id] = fetch(client, parent_id)
	parent = cache[parent_id]
	return bool((parent if isinstance(parent, dict) else raw).get("isArchived"))


def failures(cache: dict) -> list[tuple[str, BeeperError]]:
	return [(chat_id, e) for chat_id, e in cache.items() if isinstance(e, BeeperError)]


def fetch(client, chat_id: str) -> dict | BeeperError:
	try:
		return client.chat(chat_id)
	except BeeperAuthError:
		raise
	except BeeperError as e:
		frappe.logger("chatsync").warning(f"Merged member {chat_id} could not be loaded: {e}")
		return e
