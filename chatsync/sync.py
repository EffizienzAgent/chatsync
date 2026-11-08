from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from chatsync.beeper import BeeperAuthError, BeeperClient, BeeperError
from chatsync.matching import counterpart, match_chat, phone_index
from chatsync.messages import sort_order, to_site_time, upsert

EDIT_WINDOW_DAYS = 2


def client_for(account, timeout: int | None = None) -> BeeperClient:
	options = {"timeout": timeout} if timeout else {}
	return BeeperClient(account.base_url, account.get_password("access_token"), **options)


def enqueue(method: str, job_id: str, **kwargs):
	frappe.enqueue(
		method,
		queue="long",
		job_id=job_id,
		deduplicate=True,
		now=frappe.flags.in_test,
		enqueue_after_commit=not frappe.flags.in_test,
		**kwargs,
	)


def sync_all() -> None:
	for account in frappe.get_all("Chat Account", filters={"enabled": 1}, pluck="name"):
		enqueue("chatsync.sync.sync_account", f"chatsync-sync-{account}", account=account)


def commit() -> None:
	if not frappe.flags.in_test:
		frappe.db.commit()


def rollback() -> None:
	if not frappe.flags.in_test:
		frappe.db.rollback()


def kept(chat: str) -> bool:
	"""Commits; when the chat was deleted meanwhile (unassigned), drops the messages written for it."""
	commit()
	if frappe.db.exists("Chat", chat):
		return True
	frappe.db.delete("Chat Message", {"chat": chat})
	commit()
	return False


def fail(account: str | None, error: Exception) -> None:
	frappe.log_error(title=f"Chat Sync failed: {error}")
	if account:
		frappe.db.set_value("Chat Account", account, "last_error", str(error)[:140], update_modified=False)
		commit()


def sync_account(account: str) -> None:
	doc = frappe.get_doc("Chat Account", account)
	if not doc.enabled:
		return
	started = now_datetime()
	days = frappe.db.get_single_value("Chat Sync Settings", "initial_days") or 14
	threshold = started - timedelta(days=days)
	if doc.last_synced_at:
		threshold = max(threshold, get_datetime(doc.last_synced_at) - timedelta(days=EDIT_WINDOW_DAYS))
	try:
		client = client_for(doc)
		index = phone_index()
		failed: list[str] = []
		for raw in walk_chats(client, threshold):
			cp = counterpart(raw)
			if not cp:
				continue
			found = match_chat(doc, cp, index=index)
			if not found.contact:
				continue
			try:
				chat = ensure_chat(doc, raw, cp, found.contact)
				sync_chat(client, chat, started, days)
			except BeeperAuthError:
				raise
			except Exception as e:
				failed.append(str(e))
				rollback()
				frappe.log_error(title=f"Chat Sync failed for chat {cp.beeper_chat_id}: {e}")
				continue
			kept(chat)
		queue_incomplete_histories(account)
	except BeeperError as e:
		fail(account, e)
		return
	if failed:
		frappe.db.set_value(
			"Chat Account",
			account,
			"last_error",
			f"{len(failed)} chat(s) failed, last: {failed[-1]}"[:140],
			update_modified=False,
		)
		commit()
		return
	frappe.db.set_value(
		"Chat Account",
		account,
		{"last_synced_at": started, "initial_sync_done": 1, "last_error": None},
		update_modified=False,
	)
	commit()


def queue_incomplete_histories(account: str) -> None:
	for chat in frappe.get_all(
		"Chat", filters={"account": account, "history_requested": 1, "history_complete": 0}, pluck="name"
	):
		enqueue("chatsync.sync.import_history", f"chatsync-history-{chat}", chat=chat)


def walk_chats(client: BeeperClient, threshold: datetime):
	cursor = None
	while True:
		items, cursor = client.chats(cursor)
		for raw in items:
			activity = raw.get("lastActivity")
			if activity and to_site_time(activity) < threshold:
				return
			yield raw
		if not cursor or not items:
			return


def ensure_chat(account, raw: dict, cp, contact: str) -> str:
	values = {
		"user": account.user,
		"network": cp.network,
		"title": raw.get("title"),
		"counterpart_id": cp.participant_id,
		"counterpart_phone": cp.phone,
		"counterpart_username": cp.username,
		"contact": contact,
	}
	name = frappe.db.get_value("Chat", {"account": account.name, "beeper_chat_id": cp.beeper_chat_id})
	if name:
		frappe.db.set_value("Chat", name, values, update_modified=False)
		return name
	doc = frappe.get_doc(
		{"doctype": "Chat", "account": account.name, "beeper_chat_id": cp.beeper_chat_id, **values}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def sync_chat(client: BeeperClient, chat: str, started: datetime, initial_days: int) -> None:
	beeper_chat_id, newest = frappe.db.get_value("Chat", chat, ["beeper_chat_id", "newest_sort_key"])
	window_start = started - timedelta(days=EDIT_WINDOW_DAYS)
	initial_start = started - timedelta(days=initial_days)
	seen: set[str] = set()
	newest_seen = newest
	cursor = None
	while True:
		items, cursor = client.messages(beeper_chat_id, cursor)
		reached_known = False
		for raw in items:
			sent_at = to_site_time(raw["timestamp"])
			if newest:
				known = sort_order(raw.get("sortKey")) <= sort_order(newest)
				reached_known = reached_known or (known and sent_at < window_start)
			elif sent_at < initial_start:
				reached_known = True
				continue
			seen.add(raw["id"])
			upsert(chat, raw)
			if not newest_seen or sort_order(raw.get("sortKey")) > sort_order(newest_seen):
				newest_seen = raw.get("sortKey")
		if reached_known or not cursor or not items:
			break
	if seen:
		remove_vanished(chat, window_start, seen)
	update_cursor(chat, newest_seen)


def remove_vanished(chat: str, window_start: datetime, seen: set[str]) -> None:
	rows = frappe.get_all(
		"Chat Message",
		filters={"chat": chat, "sent_at": [">=", window_start]},
		fields=["name", "beeper_message_id"],
	)
	for row in rows:
		if row.beeper_message_id not in seen:
			frappe.delete_doc("Chat Message", row.name, ignore_permissions=True, force=True)


def update_cursor(chat: str, newest_sort_key: str | None) -> None:
	last = frappe.db.get_value("Chat Message", {"chat": chat}, "sent_at", order_by="sent_at desc")
	frappe.db.set_value(
		"Chat",
		chat,
		{"newest_sort_key": newest_sort_key, "last_message_at": last},
		update_modified=False,
	)


def import_history(chat: str) -> None:
	"""Pages back through the whole chat, committing each page; a cut-off run resumes at its cursor."""
	values = frappe.db.get_value(
		"Chat", chat, ["beeper_chat_id", "account", "newest_sort_key", "history_cursor"]
	)
	if not values:
		return
	beeper_chat_id, account, newest, cursor = values
	doc = frappe.get_doc("Chat Account", account)
	try:
		client = client_for(doc)
		while True:
			items, cursor = client.messages(beeper_chat_id, cursor)
			for raw in items:
				upsert(chat, raw)
				if not newest or sort_order(raw.get("sortKey")) > sort_order(newest):
					newest = raw.get("sortKey")
			update_cursor(chat, newest)
			done = not cursor or not items
			frappe.db.set_value(
				"Chat",
				chat,
				{"history_cursor": None if done else cursor, "history_complete": int(done)},
				update_modified=False,
			)
			if not kept(chat) or done:
				return
	except BeeperError as e:
		fail(account, e)


def rematch_contact(contact: str) -> None:
	index = phone_index()
	for account in frappe.get_all("Chat Account", filters={"enabled": 1}, fields=["name", "user"]):
		doc = frappe.get_doc("Chat Account", account.name)
		try:
			for raw in all_chats(client_for(doc)):
				cp = counterpart(raw)
				if not cp or match_chat(doc, cp, index=index).contact != contact:
					continue
				chat = ensure_chat(doc, raw, cp, contact)
				if not frappe.db.get_value("Chat", chat, "history_complete"):
					frappe.db.set_value("Chat", chat, "history_requested", 1, update_modified=False)
					enqueue("chatsync.sync.import_history", f"chatsync-history-{chat}", chat=chat)
		except BeeperError as e:
			fail(account.name, e)


def all_chats(client: BeeperClient):
	cursor = None
	while True:
		items, cursor = client.chats(cursor)
		yield from items
		if not cursor or not items:
			return


def identity(doc) -> tuple:
	phones = sorted(row.phone or "" for row in doc.get("phone_nos") or [])
	identifiers = sorted(
		(r.network or "", r.beeper_chat_id or "", r.identifier or "", bool(r.excluded))
		for r in doc.get("chat_identifiers") or []
	)
	return phones, identifiers


def on_contact_update(doc, method=None) -> None:
	if frappe.flags.in_import or frappe.flags.in_carddav_import or frappe.flags.in_chat_assignment:
		return
	phones, identifiers = identity(doc)
	if not (phones or identifiers):
		return
	before = doc.get_doc_before_save()
	if before is not None and identity(before) == (phones, identifiers):
		return
	if frappe.db.exists("Chat Account", {"enabled": 1}):
		enqueue("chatsync.sync.rematch_contact", f"chatsync-rematch-{doc.name}", contact=doc.name)
