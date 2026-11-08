# Extending chatsync

chatsync works on a plain Frappe site. An app that has its own idea of which contacts are
relevant plugs in through one hook. The last installed app that sets it wins.

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
stored. Without the hook every contact qualifies.

It does not affect chats assigned by hand ("Assign chat" on the Contact) or contacts the user cannot
read: matching only considers contacts the chat's owner has read permission for.

The function runs once per candidate contact on every sync, so keep it cheap.

## Settings

`Chat Sync Settings` holds the site-wide defaults: *Phone Region* (used to turn numbers without a
country code into E.164, default `DE`) and *Initial Days* (how far back the first sync of a chat
reaches, default 14).

## Archiving

When [docarchive](https://github.com/EffizienzAgent/docarchive) is installed, a conversation can be
archived as a PDF from the chat view. Documents come with `source_system` set to `chatsync` and the
contact as the source party. chatsync registers itself in docarchive's `docarchive_assign` hook to
set the counterparty to that contact.
