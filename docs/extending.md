# Extending chatsync

chatsync works on a plain Frappe site. An app that has its own idea of which contacts are
relevant plugs in through a hook (the last installed app that sets it wins); apps that cache chat
lists can follow chats being marked done.

```python
# my_app/hooks.py
required_apps = ["EffizienzAgent/chatsync"]

chatsync_contact_filter = "my_app.chat.qualifies"
```

## Contact filter: `chatsync_contact_filter`

A dotted path to a function `qualifies(contact) -> bool`. `contact` is the name of a `Contact`.

```python
import frappe


def qualifies(contact: str) -> bool:
	return bool(frappe.db.exists("Dynamic Link", {"parent": contact, "link_doctype": "Customer"}))
```

The filter applies to **phone matching** only. A chat whose number belongs to several contacts is
matched when exactly one of them qualifies; when none does, the chat stays unmatched and is not
stored. Without the hook every contact qualifies. A chat that is already stored keeps its contact
even when that contact stops qualifying later.

An unassigned chat whose number belongs to exactly one readable contact that does not qualify
carries that contact as `existing_contact` in `chatsync.api.unassigned_chats`, so a UI can offer to
assign it instead of creating a duplicate contact.

The filter does not apply to chats assigned by hand ("Assign chat" on the Contact). Contacts the
chat's owner cannot read are excluded by a separate permission check, independent of the filter.

The function runs for each candidate contact whenever a chat is matched by phone number, so keep it cheap.

## Chat done: `chatsync_chat_done`

A list of dotted paths to functions `on_done(user, beeper_chat_id, done)`, called after
`chatsync.api.set_chat_done` archived (`done=True`) or unarchived (`done=False`) a chat in Beeper.
Apps that cache chat lists (for example unassigned chats, which are never stored) update them here
so the change shows at once.

```python
# my_app/hooks.py
chatsync_chat_done = ["my_app.chat.on_done"]
```

## Group parties: `chatsync_group_parties`

A dotted path (the last app that sets it wins) to a function `parties(contacts) -> list` that
returns the parties a group chat should be attached to, given the contacts its members matched:
`(party_type, party)` tuples or `{"party_type": …, "party": …}` dicts. chatsync dedupes them and
drops the ones the user cannot read; the user confirms them in *Assign group*. Without the hook no
party is suggested.

```python
# my_app/hooks.py
chatsync_group_parties = "my_app.chat.group_parties"
chatsync_group_party_type = "Customer"


# my_app/chat.py
def group_parties(contacts):
	links = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Contact", "parent": ["in", contacts], "link_doctype": "Customer"},
		pluck="link_name",
	)
	return [("Customer", name) for name in links]
```

`chatsync_group_party_type` names the doctype *Assign group* offers when nothing is suggested
(available in the browser as `frappe.boot.chatsync_group_party_type`). Stored groups keep their
parties in the child table `Chat Party` (`party_type`, `party`) and their members in `Chat Member`
(`contact`, `participant_id`, `display_name`, `phone`), so apps can show them on the party's or the
member's timeline. The shared dialog is `chatsync.ui.assign_group(item, on_done)`, where `item` is
an unassigned group from `chatsync.api.unassigned_page` or `find_unassigned_chats`.

## Settings

`Chat Sync Settings` holds the site-wide defaults: *Phone Region* (used to turn numbers without a
country code into E.164, default `DE`) and *Initial Days* (how far back the first sync of a chat
reaches, default 14).

## Archiving

When [docarchive](https://github.com/EffizienzAgent/docarchive) is installed, a conversation can be
archived as a PDF from the chat view. Documents come with `source_system` set to `chatsync` and the
contact as the source party. chatsync registers itself in docarchive's `docarchive_assign` hook to
set the counterparty to that contact.
