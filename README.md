Chat history from Beeper for the Frappe Framework

<img src="docs/images/logo.png" alt="" width="96" align="right">

# chatsync

**chatsync** is a Frappe v16 app that shows the direct-chat history of your contacts on their
`Contact` and lets you answer from the site. It connects to a self-hosted
[Beeper](https://www.beeper.com) Server through its Client API, so WhatsApp, Signal and the other
networks Beeper bridges end up in one place.

- **One account per user.** Each user adds a **Chat Account** with the Beeper Server's base URL and
  a token. Chats and messages are visible only to their owner (see Privacy).
- **Direct chats only.** Group chats are ignored. A chat merged in Beeper (one person across
  several networks) is read as its member chats: each member is matched, stored and assigned on its
  own, with its own network, so a merged Signal + Telegram chat can yield one matched Signal chat
  and one unassigned Telegram chat. The merged chat itself is never stored.
- **Matching.** A direct chat whose counterpart exposes a phone number (typically WhatsApp and
  Signal) is matched to a contact by that number; all other chats are assigned by hand ("Assign
  chat" on the Contact). Chats without a matching contact are never stored.
- **Conversations.** *Chat Sync → Conversations* (`/desk/chat-view`) lists your conversations,
  newest first, with search. All chats with one contact (WhatsApp, Signal, LinkedIn, …) show as one
  conversation, each message marked with its network; replies go to the chat chosen next to the
  input (by default the one the contact last wrote in). On small screens list and conversation
  show one at a time.
- **From the contact.** The **Chat** button in the contact's activity opens the conversation, or,
  when there is none yet, starts a WhatsApp or Signal chat with one of the contact's phone numbers
  (nothing is sent until you write the first message).
- **Chat view.** Read the history, play voice notes, view images and download files, send text
  messages, archive a conversation. Message types the view cannot show (location, contact cards)
  appear as a placeholder naming the type; stickers show as images. Formatting is shown: HTML from Beeper is sanitized to a small
  allowlist (paragraphs, bold, italic, lists, quotes, code, http/https/mailto links), plain text
  gets WhatsApp markup (`*bold*`, `_italic_`, `~strike~`, `` `code` ``, ```` ```mono``` ````), links
  and line breaks.
- **Images and files.** Streamed from the Beeper Server on demand and never stored. Images
  (JPEG, PNG, GIF, WebP, checked by their bytes) are shown inline, MP4/WebM videos play inline,
  everything else (SVG included) is offered as a download. Files over 50 MB are not
  streamed ("open it in Beeper"); seeking in audio and video is passed on to the Beeper Server.
- **Voice notes.** Streamed from the Beeper Server on demand and never stored. Transcription on
  demand through Mistral.
- **Archiving.** Optionally save a conversation as a PDF to
  [docarchive](https://github.com/EffizienzAgent/docarchive).

## Installation

```bash
bench get-app https://github.com/EffizienzAgent/chatsync --branch version-16
bench --site <site> install-app chatsync
```

**Compatibility:** Frappe v16 (Python 3.14). The `version-16` branch always points to the latest
release; tagged releases and the changelog are on the
[releases page](https://github.com/EffizienzAgent/chatsync/releases).

The install creates the role **Chat User**. Grant it to everyone who connects an account.

## Setup

1. Run a Beeper Server that your Frappe site can reach, with your chat networks connected. For
   Docker Compose setups (e.g. frappe_docker) see [docs/docker.md](docs/docker.md): image,
   service, headless sign-in and security notes.
2. Get an access token for it. On a headless server, use the `beeper` CLI against the server:
   `beeper auth email start` sends a code to your Beeper e-mail address, `beeper auth email response`
   completes the login with that code, and the new device is then verified by comparing emoji with a
   device that is already logged in. The resulting token is what you enter below.
3. Open **Chat Account** and add the user, the server's base URL and the token, tick *Enabled*.
   Users with the role **Chat User** see only their own account.
4. Optional: **Chat Sync Settings** sets the phone region for numbers without a country code
   (default `DE`), the initial import window in days (default 14) and transcription (below).
5. Open a contact that has a matching phone number, or use **Assign chat** on the Contact for other
   networks, and open the chat view.

The scheduler syncs every two minutes. Token and transcription keys are stored as Frappe passwords.

## How chats are matched

- A chat that is already assigned to a contact stays with it. A manual assignment is stored as a row
  in the contact's chat identifiers.
- Otherwise a chat whose counterpart exposes a phone number (typically WhatsApp and Signal) is matched by it: contact numbers and the chat's
  number are normalised to E.164 with the region from *Chat Sync Settings*. Exactly one readable
  contact must match; two matches are ambiguous and left alone.
- Chats without a phone number, and chats that phone matching does not find, are matched only through
  stored chat identifiers (the chat ID, or the username or participant ID of a manual assignment).
  Use **Assign chat** on the Contact, or create a new
  contact from an unassigned chat.
- Unassigning a chat removes its messages and marks the chat as excluded for that contact, so it is
  not matched again.
- Unassigned direct chats (`chatsync.api.unassigned_chats`) can be previewed by their owner
  (`chatsync.api.preview_chat`: the newest messages, fetched live from Beeper and not stored; media
  are named, not loaded; group chats and assigned chats are refused) and ignored
  (`chatsync.api.ignore_chat`), for example private chats. Only the chat ID is kept, on the user's
  Chat Account; an ignored chat no longer shows up there. When the chat's number belongs to exactly
  one readable contact that phone matching skips (see the contact filter), the entry names it as
  `existing_contact`, so it can be assigned instead of creating a duplicate.
- Apps can restrict which contacts qualify for phone matching, see
  [docs/extending.md](docs/extending.md).

What gets imported:

- The first sync of a chat takes the last 14 days (*Initial Days*).
- Assigning a chat, or adding a new phone number to a contact, imports the full history in the
  background.
- Edits and deletions of the last two days are mirrored on every sync.

## Privacy

- Only chats with a matching or assigned contact are stored. Messages of everything else never
  reach the database.
- Chats and messages are visible only to their owner. The exception is the built-in
  **Administrator** user, as which the scheduled sync runs. The **System Manager** role has no
  bypass; it can manage Chat Accounts but not read other users' chats.
- The **Chat User** role can only read chats and messages. Every change (assigning, unassigning,
  sending, transcribing, archiving) goes through chatsync's own endpoints, and those that change an
  existing chat are allowed for its owner only.
- Voice notes, images and files are fetched from the Beeper Server when opened and are not stored
  (no File record, nothing on disk). Only their name, type and size are saved with the message, and
  for voice notes the transcript. Messages synced before these details were kept show their first
  attachment without name and size.
- Sending, and starting a chat from a contact, are possible only in a browser session. Requests that carry an
  `Authorization` header (API keys, OAuth), and requests without the session's CSRF token, are
  refused, and there is no background or MCP path to send. Starting a chat sends no message.
- Transcription sends the audio to Mistral (*Chat Sync Settings* → provider, API key and model).
  Leave the key empty to switch it off.
- Archiving needs a docarchive role; the PDF contains the messages of the chosen period, with
  images and files listed by name, not embedded. Archived
  PDFs follow docarchive's access rules, not the owner-only rule above.

## Extending

Apps can decide which contacts take part in phone matching with the hook `chatsync_contact_filter`.
See [docs/extending.md](docs/extending.md).

## Development

```bash
bench --site your.site set-config allow_tests true
bench --site your.site run-tests --app chatsync
```

## License

MIT, see [LICENSE](LICENSE). Frappe is a trademark of Frappe Technologies and Beeper of its owner;
this project is not affiliated with either.

## Professional setup & support

Need help setting this up or integrating it into your Frappe stack? Write to oss@effizienzagent.de (English or German). German-speaking SMEs: see [mittelstandshub.de](https://mittelstandshub.de).
