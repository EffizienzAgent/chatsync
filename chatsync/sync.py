from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from chatsync.beeper import BeeperAuthError, BeeperClient, BeeperError
from chatsync.matching import counterpart, match_chat, phone_index
from chatsync.merges import archived, expand, failures, listed_states
from chatsync.messages import sort_order, to_site_time, upsert

EDIT_WINDOW_DAYS = 2
STATE_CHECK_MINUTES = 10
STATE_CHECKS_PER_RUN = 20
STATE_FIELDS = ["name", "beeper_chat_id", "beeper_archived", "unread_count", "beeper_state_checked_at"]


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


def set_error(account: str, error: str) -> bool:
	"""Stores the account's last error; True when it changed. Only a change is worth an Error Log,
	so a Beeper Server that stays down does not log every two minutes."""
	error = error[:140]
	if frappe.db.get_value("Chat Account", account, "last_error") == error:
		return False
	frappe.db.set_value("Chat Account", account, "last_error", error, update_modified=False)
	commit()
	return True


def fail(account: str, error: Exception) -> None:
	if set_error(account, str(error)):
		frappe.log_error(title=f"Chat Sync failed: {error}")


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
		failed: list[tuple[str, str, str]] = []
		members: dict = {}
		listed: dict = {}
		seen: set[str] = set()
		for raw in walk_members(client, threshold, members, listed):
			cp = counterpart(raw)
			if not cp:
				continue
			found = match_chat(doc, cp, index=index)
			if not found.contact:
				continue
			try:
				chat = ensure_chat(doc, raw, cp, found.contact)
				sync_chat(client, chat, started, days)
				current = frappe.db.get_value("Chat", chat, STATE_FIELDS, as_dict=True)
				store_state(current, archived(client, raw, members), raw.get("unreadCount") or 0)
				seen.add(chat)
			except BeeperAuthError:
				raise
			except Exception as e:
				failed.append((cp.beeper_chat_id, str(e), frappe.get_traceback()))
				rollback()
				continue
			kept(chat)
		failed += [(chat_id, str(e), "") for chat_id, e in failures(members)]
		refresh_unwalked(client, account, seen, members, listed)
		queue_incomplete_histories(account)
	except BeeperError as e:
		fail(account, e)
		return
	if failed:
		if set_error(account, f"{len(failed)} chat(s) failed, last: {failed[-1][1]}"):
			for chat_id, error, traceback in failed:
				frappe.log_error(title=f"Chat Sync failed for chat {chat_id}: {error}", message=traceback)
			commit()
		return
	frappe.db.set_value(
		"Chat Account",
		account,
		{"last_synced_at": started, "initial_sync_done": 1, "last_error": None},
		update_modified=False,
	)
	commit()


def state_stale(checked_at) -> bool:
	return not checked_at or get_datetime(checked_at) <= now_datetime() - timedelta(
		minutes=STATE_CHECK_MINUTES
	)


def store_state(current, is_archived: bool, unread_count: int | None = None) -> None:
	"""Writes only what changed; the check time only when it is due, so it throttles single checks."""
	values = {}
	if bool(current.beeper_archived) != bool(is_archived):
		values["beeper_archived"] = int(is_archived)
	if unread_count is not None and (current.unread_count or 0) != unread_count:
		values["unread_count"] = unread_count
	if state_stale(current.beeper_state_checked_at):
		values["beeper_state_checked_at"] = now_datetime()
	if values:
		frappe.db.set_value("Chat", current.name, values, update_modified=False)


def refresh_unwalked(client: BeeperClient, account: str, seen: set[str], cache: dict, listed: dict) -> None:
	"""Chats can be archived in Beeper without new activity. Stored chats the walk did not sync take their
	state from the chat list pages already fetched; open ones not listed there are asked for, each at most
	every STATE_CHECK_MINUTES and at most STATE_CHECKS_PER_RUN per run, the longest unchecked first."""
	rows = frappe.get_all(
		"Chat",
		filters={"account": account, "name": ["not in", list(seen) or [""]]},
		or_filters={"beeper_archived": 0, "beeper_chat_id": ["in", list(listed) or [""]]},
		fields=STATE_FIELDS,
	)
	due = []
	for row in rows:
		if row.beeper_chat_id in listed:
			state = listed[row.beeper_chat_id]
			store_state(row, state["archived"], state["unread_count"])
		elif state_stale(row.beeper_state_checked_at):
			due.append(row)
	due.sort(
		key=lambda r: get_datetime(r.beeper_state_checked_at) if r.beeper_state_checked_at else datetime.min
	)
	for row in due[:STATE_CHECKS_PER_RUN]:
		try:
			raw = client.chat(row.beeper_chat_id)
		except BeeperAuthError:
			raise
		except BeeperError as e:
			frappe.logger("chatsync").warning(f"State of chat {row.beeper_chat_id} could not be loaded: {e}")
			continue
		store_state(row, archived(client, raw, cache), raw.get("unreadCount") or 0)
	commit()


def queue_incomplete_histories(account: str) -> None:
	for chat in frappe.get_all(
		"Chat", filters={"account": account, "history_requested": 1, "history_complete": 0}, pluck="name"
	):
		enqueue("chatsync.sync.import_history", f"chatsync-history-{chat}", chat=chat)


def walk_chats(client: BeeperClient, threshold: datetime, listed: dict | None = None):
	"""Chats active since `threshold`, newest first. `listed` collects the state of every chat on the
	fetched pages, including those past the threshold."""
	cursor = None
	while True:
		items, cursor = client.chats(cursor)
		if listed is not None:
			listed.update(listed_states(items))
		for raw in items:
			activity = raw.get("lastActivity")
			if activity and to_site_time(activity) < threshold:
				return
			yield raw
		if not cursor or not items:
			return


def walk_members(
	client: BeeperClient, threshold: datetime, cache: dict | None = None, listed: dict | None = None
):
	"""Like walk_chats, with merged chats replaced by their members active since `threshold`."""
	cache = {} if cache is None else cache
	seen: set[str] = set()
	for raw in walk_chats(client, threshold, listed):
		for chat in expand(client, raw, cache):
			activity = chat.get("lastActivity")
			if chat["id"] in seen or (activity and to_site_time(activity) < threshold):
				continue
			seen.add(chat["id"])
			yield chat


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
	frappe.db.set_value("Chat", chat, "newest_sort_key", newest_sort_key, update_modified=False)
	update_activity(chat)


def update_activity(chat: str) -> None:
	"""When the chat last saw a message, and one in each direction (does it wait for an answer?)."""
	table = frappe.qb.DocType("Chat Message")
	newest = dict(
		frappe.qb.from_(table)
		.select(table.direction, frappe.qb.functions("MAX", table.sent_at))
		.where(table.chat == chat)
		.groupby(table.direction)
		.run()
	)
	values = {"last_incoming_at": newest.get("In"), "last_outgoing_at": newest.get("Out")}
	values["last_message_at"] = max(filter(None, values.values()), default=None)
	frappe.db.set_value("Chat", chat, values, update_modified=False)


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
	cache: dict = {}
	seen: set[str] = set()
	cursor = None
	while True:
		items, cursor = client.chats(cursor)
		for raw in items:
			for chat in expand(client, raw, cache):
				if chat["id"] not in seen:
					seen.add(chat["id"])
					yield chat
		if not cursor or not items:
			return


def identity(doc) -> tuple:
	phones = sorted(row.phone or "" for row in doc.get("phone_nos") or [])
	identifiers = sorted(
		(r.network or "", r.beeper_chat_id or "", r.identifier or "", bool(r.excluded))
		for r in doc.get("chat_identifiers") or []
	)
	return phones, identifiers


def keep_chat_identifiers(doc, method=None) -> None:
	"""Rows are written by assignments and syncs only; a form that missed them must not delete or add any."""
	if doc.is_new() or frappe.flags.in_chat_assignment:
		return
	rows = frappe.get_all(
		"Chat Identifier",
		filters={"parenttype": "Contact", "parentfield": "chat_identifiers", "parent": doc.name},
		fields=["*"],
		order_by="idx",
	)
	doc.set("chat_identifiers", rows)


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
