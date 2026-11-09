# Changelog

## [0.3.1](https://github.com/EffizienzAgent/chatsync/compare/v0.3.0...v0.3.1) (2026-11-09)


### Bug Fixes

* dedupe merged members, keep the sync mark on member failures ([4aa9f0c](https://github.com/EffizienzAgent/chatsync/commit/4aa9f0c76a8efd2c27ca0480102bceacbe83adac))
* read Beeper merged chats as their member chats ([eaa2b2c](https://github.com/EffizienzAgent/chatsync/commit/eaa2b2ccc746eebf64975a7b6dbbb325b6c040ec))

## [0.3.0](https://github.com/EffizienzAgent/chatsync/compare/v0.2.0...v0.3.0) (2026-11-08)


### Features

* chat action on the contact timeline that opens or starts a chat ([228a53c](https://github.com/EffizienzAgent/chatsync/commit/228a53c0d96b1e74bbfdb36030d9612ca517f35b))
* list conversations for the chat inbox ([bfd6002](https://github.com/EffizienzAgent/chatsync/commit/bfd60020441c9f648cf8552b79bd63261bb37895))
* logo, apps screen entry and social preview ([4ec87a0](https://github.com/EffizienzAgent/chatsync/commit/4ec87a042dadff5ac6e81298f386f28d32a9eeba))
* merged messages of all chats with a contact ([7479e4e](https://github.com/EffizienzAgent/chatsync/commit/7479e4eb3407b658d033ed30a0e5272d56d2b2d1))
* start chats from a contact, preview unassigned chats, keep Beeper type of unsupported messages ([023f658](https://github.com/EffizienzAgent/chatsync/commit/023f658786a4dda9934e07a46d1472a32a595d9e))
* two-pane chat inbox with merged conversations per contact ([335c316](https://github.com/EffizienzAgent/chatsync/commit/335c3165a4aab824efd93dffccdbebb6055f0a20))


### Bug Fixes

* polish chat view after visual check (markup-free previews, sticky bottom, dark mode chips) ([feb7a2f](https://github.com/EffizienzAgent/chatsync/commit/feb7a2f19b6d92bf4a4a8293e55d3b021174fc70))
* start_chat keeps chats of other contacts, escapes Beeper strings in the UI ([b4849fa](https://github.com/EffizienzAgent/chatsync/commit/b4849fa2ebd24540a7f86768ec78d0660aee5b83))


### Documentation

* Docker Compose guide for Beeper Server, README for conversations and chat start ([c74f9b8](https://github.com/EffizienzAgent/chatsync/commit/c74f9b89ad41ed179d216c463346e1f7b5039821))

## [0.2.0](https://github.com/EffizienzAgent/chatsync/compare/v0.1.0...v0.2.0) (2026-11-08)


### Features

* render Beeper HTML and WhatsApp markup safely in the chat view ([a9d2d3e](https://github.com/EffizienzAgent/chatsync/commit/a9d2d3e6354e41e9e9aed8cd8124547e4affb70c))
* show images, videos and files in the chat view ([dfdc088](https://github.com/EffizienzAgent/chatsync/commit/dfdc088e08f7a9649ae6668f8ebe411e7b611817))
* store all attachments and stream them via chatsync.media.serve ([57db6bb](https://github.com/EffizienzAgent/chatsync/commit/57db6bbdb3baccfc069c2555ec14acf6022a097a))


### Bug Fixes

* cap streamed attachments at 50 MB and forward Range to Beeper ([84545ab](https://github.com/EffizienzAgent/chatsync/commit/84545ab196c2c9c7d3ef977374a0fece73113bed))
* forward only single byte ranges to Beeper and name the size on 416 ([43f1487](https://github.com/EffizienzAgent/chatsync/commit/43f1487152f6d3cc222f459ab7286e2092ee6c4c))
* keep only absolute links, real markup and intact code in formatting ([624a841](https://github.com/EffizienzAgent/chatsync/commit/624a84192f9e9918bf465acefe2dc63f123d00a3))

## [0.1.0](https://github.com/EffizienzAgent/chatsync/compare/v0.0.1...v0.1.0) (2026-11-08)


### Features

* archive a chat transcript on demand ([e4b66cc](https://github.com/EffizienzAgent/chatsync/commit/e4b66cc63af5e0ceba09bd22d182cda78d34e7c3))
* assign and unassign chats ([4f61fbe](https://github.com/EffizienzAgent/chatsync/commit/4f61fbed8898cd02d065addd6328df9722cd4d8f))
* Beeper Client API wrapper ([7f53e6a](https://github.com/EffizienzAgent/chatsync/commit/7f53e6a6abdc67c5b5c1f92b754ffa08284a54bd))
* chat doctypes and owner-only access ([6936788](https://github.com/EffizienzAgent/chatsync/commit/6936788807cc0f18b76052cafa365c1aea57a77c))
* chat view with reply ([eb2c699](https://github.com/EffizienzAgent/chatsync/commit/eb2c6995574977096483ed17493d378d0280b0bf))
* ignore unassigned chats and name an existing private contact ([4e6591f](https://github.com/EffizienzAgent/chatsync/commit/4e6591f98e8f14f4c83b1f690b3cd1b819617de4))
* match chat counterparts to contacts ([913ac15](https://github.com/EffizienzAgent/chatsync/commit/913ac15c5d4d1e3cbca2bbf7ee7e040c8e3c2e00))
* non-throwing core for unassigned chats with a short timeout ([501781a](https://github.com/EffizienzAgent/chatsync/commit/501781a1fdb26dd110560370878fdb75fa55261c))
* play and transcribe voice messages ([4193643](https://github.com/EffizienzAgent/chatsync/commit/41936437f2480201e510f813687a276891cc6a6e))
* sync matched direct chats from Beeper ([b148568](https://github.com/EffizienzAgent/chatsync/commit/b1485686a930835349ca3a0b8bcf3914449cdcb6))


### Bug Fixes

* chat users read chats only; changes are for the owner ([84766a9](https://github.com/EffizienzAgent/chatsync/commit/84766a96e3d6cd17986eb09fcf3e852fe8811492))
* chat view review round 1 ([ef02884](https://github.com/EffizienzAgent/chatsync/commit/ef028845221c11ccb67a260fecc09388a00c5761))
* chat view review round 2 ([a6ef818](https://github.com/EffizienzAgent/chatsync/commit/a6ef8189cb0424b6c4a8fa96ed17e66c24d36606))
* commit history per page and drop messages of chats deleted meanwhile ([0c582c7](https://github.com/EffizienzAgent/chatsync/commit/0c582c73308c32577fd4665b740a0bb485a6bee1))
* drop the in_job permission bypass ([f61043f](https://github.com/EffizienzAgent/chatsync/commit/f61043f96731c312fa8808c0b955771e8d6867e4))
* English label for the Contact chat identifier fields ([812e41b](https://github.com/EffizienzAgent/chatsync/commit/812e41b0a55d8b7eed39c46830026f15f12f020a))
* harden Beeper sync (after-commit enqueue, per-chat isolation, race-safe upsert) ([2261923](https://github.com/EffizienzAgent/chatsync/commit/2261923f655275211384c58a2f54112ca86d77ba))
* keep chat identifier rows server-owned so stale Contact forms cannot drop them ([7f7ccbe](https://github.com/EffizienzAgent/chatsync/commit/7f7ccbe8d4725eaf1f6134cb5cc5af2ce490c1f1))
* log sync errors only when the account's error changes ([ddad30b](https://github.com/EffizienzAgent/chatsync/commit/ddad30b1230d56023123177e84177770915ae4be))
* open the chat view under /desk like every other link ([4b4fb3f](https://github.com/EffizienzAgent/chatsync/commit/4b4fb3fda533c0f2a89ed922d436af33f44e43f3))
* refuse creating chats and messages under another user's parent ([7468563](https://github.com/EffizienzAgent/chatsync/commit/746856359f40338e783ed78a4ba958eba32cea67))
* refuse sending from sessions without a CSRF token ([a96646f](https://github.com/EffizienzAgent/chatsync/commit/a96646fded00d6989367db65992ac113b1237419))
* rematch contacts changed by a regular CardDAV pull ([d9a9fcd](https://github.com/EffizienzAgent/chatsync/commit/d9a9fcd486d6bb7bc1cb04bedbdc2556d09b1dfa))
* replace chat identifier rows on assign, tighten ownership tests ([5c4dd75](https://github.com/EffizienzAgent/chatsync/commit/5c4dd75da12fa041e363cad7ad33f14641056ce0))
* resume a cut-off history import at its stored cursor ([d67adcc](https://github.com/EffizienzAgent/chatsync/commit/d67adcc852e59bb81c9c338b2ee19219bfba6e31))
* roll back a failed chat before syncing the next ([a4fa0bb](https://github.com/EffizienzAgent/chatsync/commit/a4fa0bbbb46b51ed26b8268ab47d63ad32db2ae8))
* run Beeper tests in app runs, wrap network errors ([38b5a4e](https://github.com/EffizienzAgent/chatsync/commit/38b5a4e029a7c0f090db7c041e86e16282c0eb4a))
* scope exclusion to its chat, index phones once per run ([42685f6](https://github.com/EffizienzAgent/chatsync/commit/42685f6dd33ae44434d5ce0341ca1615f69a701c))
* serve voice as audio only and honour byte ranges ([6060ac1](https://github.com/EffizienzAgent/chatsync/commit/6060ac18a7736c36519bb8895c339e7f64833219))
* stored chats keep syncing with their contact ([f68f19a](https://github.com/EffizienzAgent/chatsync/commit/f68f19a5f10f6df342a01d687e340815a7451b87))
* write chat identifier rows without saving the contact ([3c24636](https://github.com/EffizienzAgent/chatsync/commit/3c24636ac4fa14d0c4f6589affe3f22a8ec05215))


### Documentation

* align visibility and matching wording ([ad17ac8](https://github.com/EffizienzAgent/chatsync/commit/ad17ac893f96323e8a364ff2280cff3954df2710))
* README and extending guide ([067db1e](https://github.com/EffizienzAgent/chatsync/commit/067db1e9895addec4447d4627a2868905c62bceb))
* stored chats and existing contacts in the contact filter guide ([39fa162](https://github.com/EffizienzAgent/chatsync/commit/39fa162ee62e4a64783ccf56796224341695e4a6))
* tighten privacy and matching claims, translate voice error ([26aebdf](https://github.com/EffizienzAgent/chatsync/commit/26aebdf84c210f086040de722e56dc85a1cb5928))
