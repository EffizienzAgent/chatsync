from dataclasses import dataclass

import frappe

from chatsync.extensions import contact_filter
from chatsync.merges import is_merged
from chatsync.phone import normalize


@dataclass(frozen=True)
class Counterpart:
	network: str
	beeper_chat_id: str
	participant_id: str
	phone: str | None
	username: str | None
	name: str | None


@dataclass(frozen=True)
class Match:
	contact: str | None
	ambiguous: bool
	excluded: bool


def counterpart(chat: dict) -> Counterpart | None:
	if chat.get("type") != "single":
		return None
	if is_merged(chat):
		return None
	participants = chat.get("participants") or []
	if isinstance(participants, dict):
		participants = participants.get("items") or []
	others = [p for p in participants if not p.get("isSelf")]
	if len(others) != 1:
		return None
	other = others[0]
	return Counterpart(
		network=chat["accountID"].split(".")[0],
		beeper_chat_id=chat["id"],
		participant_id=other.get("id"),
		phone=other.get("phoneNumber"),
		username=other.get("username"),
		name=other.get("fullName"),
	)


def readable(user: str, contacts) -> list[str]:
	return [c for c in dict.fromkeys(contacts) if frappe.has_permission("Contact", "read", doc=c, user=user)]


def identifier_rows(**filters) -> list[dict]:
	return frappe.get_all(
		"Chat Identifier",
		filters={"parenttype": "Contact", **filters},
		fields=["parent", "excluded", "beeper_chat_id"],
		order_by="creation",
	)


def from_rows(user: str, rows: list[dict], chat_id: str) -> Match | None:
	rows = [r for r in rows if not r.excluded or r.beeper_chat_id in (None, "", chat_id)]
	rows = [r for r in rows if r.parent in readable(user, [r.parent for r in rows])]
	if not rows:
		return None
	if any(r.excluded for r in rows):
		return Match(None, False, True)
	contacts = list(dict.fromkeys(r.parent for r in rows))
	if len(contacts) > 1:
		return Match(None, True, False)
	return Match(contacts[0], False, False)


def phone_index() -> dict[str, list[str]]:
	region = frappe.db.get_single_value("Chat Sync Settings", "phone_region") or "DE"
	index: dict[str, list[str]] = {}
	rows = frappe.get_all("Contact Phone", filters={"parenttype": "Contact"}, fields=["parent", "phone"])
	for row in rows:
		if row.phone and (e164 := normalize(row.phone, region)):
			index.setdefault(e164, []).append(row.parent)
	return index


def phone_contacts(user: str, phone: str, index: dict[str, list[str]]) -> list[str]:
	return readable(user, index.get(phone, []))


def by_phone(user: str, phone: str, index: dict[str, list[str]]) -> Match | None:
	qualifies = contact_filter()
	contacts = [c for c in phone_contacts(user, phone, index) if qualifies(c)]
	if not contacts:
		return None
	return Match(contacts[0], False, False) if len(contacts) == 1 else Match(None, True, False)


def by_identifiers(user: str, cp: Counterpart) -> Match | None:
	found = from_rows(user, identifier_rows(beeper_chat_id=cp.beeper_chat_id), cp.beeper_chat_id)
	if found:
		return found
	for identifier in dict.fromkeys(filter(None, [cp.username, cp.participant_id])):
		found = from_rows(user, identifier_rows(network=cp.network, identifier=identifier), cp.beeper_chat_id)
		if found:
			return found
	return None


def match(user: str, cp: Counterpart, index: dict[str, list[str]] | None = None) -> Match:
	found = by_identifiers(user, cp)
	if found:
		return found
	if cp.phone:
		found = by_phone(user, cp.phone, phone_index() if index is None else index)
		if found:
			return found
	return Match(None, False, False)


def match_chat(account, cp: Counterpart, index: dict[str, list[str]] | None = None) -> Match:
	"""Like `match`, but a stored chat keeps its contact unless identifier rows say otherwise."""
	found = by_identifiers(account.user, cp)
	if found:
		return found
	stored = frappe.db.get_value(
		"Chat", {"account": account.name, "beeper_chat_id": cp.beeper_chat_id}, "contact"
	)
	if stored and frappe.db.exists("Contact", stored) and readable(account.user, [stored]):
		return Match(stored, False, False)
	return match(account.user, cp, index=index)
