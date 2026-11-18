frappe.provide("chatsync.ui");

const CONTEXT = "Chat View";
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];
const VIDEO_TYPES = ["video/mp4", "video/webm"];
const MAX_INPUT_LINES = 6;
const POLL_DELAYS = [500, 1000, 2000, 4000, 8000];
const REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
const RECONCILE_DELAY = 2000;
const RECEIPT_DELAYS = [15000, 60000, 180000];
const LIVE_INTERVAL = 15000;
const VOICE_TYPES = ["audio/ogg;codecs=opus", "audio/webm;codecs=opus", "audio/mp4"];
const VOICE_MAX_SECONDS = 15 * 60;
const RECEIPTS = {
	Sent: { icon: "check", label: "Sent" },
	Delivered: { icon: "check-check", label: "Delivered" },
	Read: { icon: "check-check", label: "Read" },
	Failed: { icon: "circle-alert", label: "Not sent" },
};

function avatar(title, url) {
	const [bg, fg] = frappe.get_palette(title || "?");
	const $avatar = $(`<div class="chat-avatar"></div>`)
		.css({ background: `var(${bg})`, color: `var(${fg})` })
		.append($(`<span></span>`).text(frappe.get_abbr(title || "?")));
	if (url)
		$(`<img alt="" loading="lazy" decoding="async">`)
			.attr("src", url)
			.on("error", (e) => $(e.currentTarget).remove())
			.appendTo($avatar);
	return $avatar;
}

function group_icon() {
	return $(`<span class="chat-group-icon"></span>`)
		.attr({ title: __("Group", null, CONTEXT), "aria-label": __("Group", null, CONTEXT) })
		.append(frappe.utils.icon("users", "xs"));
}

function group_summary(item) {
	const parts = [__("Group", null, CONTEXT), __("{0} members", [item.member_count], CONTEXT)];
	const parties = (item.suggested_parties || []).map((p) => p.title);
	if (parties.length) parts.push(__("Suggestion: {0}", [parties.join(", ")], CONTEXT));
	return parts.join(" · ");
}

/** Loads all participants of a group (list pages carry only some), then opens the assign dialog. */
function assign_group(item, on_done) {
	return frappe
		.call({
			method: "chatsync.api.group_for_assignment",
			type: "GET",
			args: { beeper_chat_id: item.beeper_chat_id },
			freeze: true,
		})
		.then((r) => group_dialog(r.message, on_done));
}

/** Asks which parties a group belongs to and which members' contacts to link, then stores it. */
function group_dialog(item, on_done) {
	const parties = (item.suggested_parties || []).map((p) => ({ ...p }));
	const party_type = parties[0]?.party_type || frappe.boot.chatsync_group_party_type || null;
	const escape = frappe.utils.escape_html;
	const party_option = (party, index) => ({ label: escape(party.title || party.party), value: String(index), checked: 1 });
	const members = (item.members || []).filter((m) => m.contact);
	const dialog = new frappe.ui.Dialog({
		title: __("Assign group: {0}", [escape(item.title || item.beeper_chat_id)], CONTEXT),
		fields: [
			{
				fieldname: "party_type",
				fieldtype: "Link",
				options: "DocType",
				label: __("Party type", null, CONTEXT),
				default: party_type,
				hidden: party_type ? 1 : 0,
			},
			{
				fieldname: "parties",
				fieldtype: "MultiCheck",
				label: party_type ? __(party_type) : __("Parties", null, CONTEXT),
				options: parties.map(party_option),
				sort_options: false,
				columns: 1,
			},
			{
				fieldname: "add_party",
				fieldtype: "Dynamic Link",
				options: "party_type",
				label: party_type ? __("Add {0}", [__(party_type)], CONTEXT) : __("Add party", null, CONTEXT),
				onchange() {
					const value = dialog.get_value("add_party");
					const type = dialog.get_value("party_type");
					if (!value || !type) return;
					const field = dialog.fields_dict.parties;
					const checked = new Set(field.get_value());
					field.df.options.forEach((option) => (option.checked = checked.has(option.value) ? 1 : 0));
					if (!parties.some((p) => p.party_type === type && p.party === value)) {
						parties.push({ party_type: type, party: value, title: frappe.utils.get_link_title(type, value) });
						field.df.options.push(party_option(parties[parties.length - 1], parties.length - 1));
					}
					field.refresh();
					dialog.set_value("add_party", "");
				},
			},
			{
				fieldname: "contacts",
				fieldtype: "MultiCheck",
				label: __("Link members to contacts", null, CONTEXT),
				options: members.map((m, index) => ({
					label:
						escape(
							m.contact_name && m.contact_name !== m.display_name
								? `${m.display_name} → ${m.contact_name}`
								: m.display_name || m.contact
						) + (m.by_name ? ` <span class="text-muted">(${escape(__("by name", null, CONTEXT))})</span>` : ""),
					value: String(index),
					checked: m.by_name ? 0 : 1,
				})),
				sort_options: false,
				columns: 1,
				hidden: members.length ? 0 : 1,
			},
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${escape(
					__("Only this group is stored, with its full history. Members' other chats are not affected.", null, CONTEXT)
				)}</p>`,
			},
		],
		primary_action_label: __("Assign group", null, CONTEXT),
		primary_action: (values) => {
			const chosen = (values.parties || []).map((index) => parties[Number(index)]);
			dialog.disable_primary_action();
			frappe
				.call({
					method: "chatsync.api.assign_group",
					args: {
						beeper_chat_id: item.beeper_chat_id,
						parties: JSON.stringify(chosen.map(({ party_type, party }) => ({ party_type, party }))),
						contacts: JSON.stringify((values.contacts || []).map((index) => members[Number(index)].contact)),
						offered: JSON.stringify(members.map((m) => m.participant_id)),
					},
				})
				.then((r) => {
					dialog.hide();
					frappe.show_alert({ message: __("Group assigned", null, CONTEXT), indicator: "green" });
					on_done?.(r.message);
				})
				.catch(() => dialog.enable_primary_action());
		},
	});
	dialog.show();
	return dialog;
}

function network_chip(label) {
	return $(`<span class="chat-network-chip"></span>`).attr("data-network", (label || "").toLowerCase()).text(label);
}

function plain_text(html) {
	return new DOMParser().parseFromString(html || "", "text/html").body.textContent;
}

function server_message(r) {
	try {
		const messages = JSON.parse(r?._server_messages || "[]").map((m) => JSON.parse(m).message);
		return plain_text(messages.join(" "));
	} catch (e) {
		return "";
	}
}

function local_time(iso) {
	return iso ? moment(iso).format("YYYY-MM-DD HH:mm:ss") : "";
}

function unsupported_label(type) {
	const names = {
		STICKER: __("Sticker", null, CONTEXT),
		LOCATION: __("Location", null, CONTEXT),
		AUDIO: __("Audio", null, CONTEXT),
	};
	const name = type ? names[type] || type.toLowerCase() : "";
	return __("Unsupported message type", null, CONTEXT) + (name ? ` (${name})` : "");
}

function compare(a, b) {
	return a.sent_at.localeCompare(b.sent_at) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
}

function is_emoji(key) {
	return /\p{Extended_Pictographic}|\p{Regional_Indicator}/u.test(key);
}

function same_reactions(a, b) {
	return JSON.stringify(a || []) === JSON.stringify(b || []);
}

function now_time() {
	return moment().format("HH:mm");
}

/** Messages, composer and sending, shared by the chat view and the quick reply on the contact.

Subclasses provide $scroll, $list (and optionally $more), and keep messages, chats, chat, mode and title. */
class ChatPane {
	init_pane() {
		this.messages = [];
		this.chats = [];
		this.ticket = 0;
		this.pending = [];
		this.timers = new Set();
		this.confirmed = new Set();
		this.staged = [];
		this.start_live();
	}

	/** While the pane is visible, its chats are refreshed from Beeper every 15 seconds: new messages and
	receipts show up without waiting for the sync. Stops by itself once the pane leaves the page. */
	start_live() {
		clearInterval(this.live_timer);
		this.live_timer = setInterval(() => {
			const scroll = this.$scroll?.[0];
			if (scroll && !scroll.isConnected) return clearInterval(this.live_timer);
			if (!scroll || document.hidden || !scroll.offsetParent || this.sending || this.mode === "preview") return;
			const ticket = this.ticket;
			(this.chats || []).slice(0, 3).forEach(({ name }) =>
				frappe
					.call({ method: "chatsync.api.refresh_messages", type: "POST", silent: true, args: { chat: name } })
					.then((r) => ticket === this.ticket && this.has_news(r.message.messages) && this.merge(r.message.messages, name))
					.catch(() => {})
			);
		}, LIVE_INTERVAL);
	}

	/** Re-rendering replaces the list (and stops a playing voice note), so only when something changed. */
	has_news(incoming) {
		const by_name = new Map(this.messages.map((m) => [m.name, m]));
		const oldest = this.has_more ? this.messages[0] : null;
		return incoming.some((m) => {
			const current = by_name.get(m.name);
			if (!current && oldest && compare(m, oldest) < 0) return false;
			return !current || (m.receipt && m.receipt !== current.receipt) || !same_reactions(current.reactions, m.reactions);
		});
	}

	make_composer($parent, $network_parent) {
		const $composer = $(`<footer class="chat-composer"></footer>`).appendTo($parent);
		this.$network = $(`<select class="form-control chat-network"></select>`)
			.attr({ "aria-label": __("Send via", null, CONTEXT), title: __("Send via", null, CONTEXT) })
			.on("change", () => this.select_chat(this.$network.val()))
			.appendTo($network_parent || $composer);
		this.$tray = $(`<div class="chat-tray"></div>`).insertBefore($composer).hide();
		this.$file = $(`<input type="file" multiple hidden>`)
			.on("change", () => {
				this.stage([...this.$file[0].files]);
				this.$file.val("");
			})
			.appendTo($composer);
		this.$attach = $(`<button type="button" class="btn btn-default chat-tool"></button>`)
			.attr({ title: __("Attach files", null, CONTEXT), "aria-label": __("Attach files", null, CONTEXT) })
			.append(frappe.utils.icon("paperclip", "sm"))
			.on("click", () => this.$file.trigger("click"))
			.appendTo($composer);
		this.$input = $(`<textarea class="form-control chat-input" rows="1"></textarea>`).appendTo($composer);
		this.$recording = $(`<div class="chat-recording" role="status"></div>`).appendTo($composer).hide();
		this.$send = $(`<button type="button" class="btn btn-primary chat-send" disabled></button>`)
			.attr({ title: __("Send", null, CONTEXT), "aria-label": __("Send", null, CONTEXT) })
			.append(frappe.utils.icon("send-horizontal", "sm"))
			.on("click", () => (this.recorder ? this.stop_recording(true) : this.send()))
			.appendTo($composer);
		this.$mic = $(`<button type="button" class="btn btn-primary chat-send chat-mic"></button>`)
			.attr({ title: __("Record a voice message", null, CONTEXT), "aria-label": __("Record a voice message", null, CONTEXT) })
			.append(frappe.utils.icon("mic", "sm"))
			.on("click", () => this.start_recording())
			.appendTo($composer)
			.hide();
		this.$input.on("input", () => this.update_composer());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey && !e.originalEvent.isComposing) {
				e.preventDefault();
				this.send();
			}
		});
		this.$input.on("paste", (e) => {
			const files = [...(e.originalEvent.clipboardData?.files || [])];
			if (!files.length) return;
			e.preventDefault();
			this.stage(files);
		});
		this.make_drop_zone($parent);
		return $composer;
	}

	/** Files dropped anywhere on the conversation are staged like picked ones. */
	make_drop_zone($zone) {
		let depth = 0;
		const has_files = (e) => [...(e.originalEvent.dataTransfer?.types || [])].includes("Files");
		$zone.on("dragenter", (e) => {
			if (!has_files(e) || !this.can_send()) return;
			e.preventDefault();
			depth++;
			$zone.addClass("chat-dropping");
		});
		$zone.on("dragover", (e) => {
			if (!has_files(e) || !this.can_send()) return;
			e.preventDefault();
			e.originalEvent.dataTransfer.dropEffect = "copy";
		});
		$zone.on("dragleave", () => {
			depth = Math.max(0, depth - 1);
			if (!depth) $zone.removeClass("chat-dropping");
		});
		$zone.on("drop", (e) => {
			depth = 0;
			$zone.removeClass("chat-dropping");
			if (!has_files(e)) return;
			e.preventDefault();
			if (this.can_send()) this.stage([...e.originalEvent.dataTransfer.files]);
		});
	}

	can_send() {
		return Boolean(this.chat) && !this.$input.prop("disabled");
	}

	stage(files) {
		if (!this.can_send()) return;
		const limit = frappe.boot.chatsync_upload_max_bytes || 25 * 1024 * 1024;
		const too_big = files.filter((f) => f.size > limit);
		if (too_big.length)
			frappe.show_alert({
				message: __("Too large to send from here (max. {0}): {1}", [this.file_size(limit), too_big.map((f) => f.name).join(", ")], CONTEXT),
				indicator: "orange",
			});
		this.staged = this.staged.concat(files.filter((f) => f.size && f.size <= limit));
		this.render_tray();
		this.update_composer();
		this.$input.focus();
	}

	render_tray() {
		this.$tray.empty().toggle(this.staged.length > 0);
		this.staged.forEach((file, index) => {
			const $chip = $(`<span class="chat-staged"></span>`).appendTo(this.$tray);
			$chip.append(frappe.utils.icon(file.type.startsWith("image/") ? "image" : "paperclip", "xs"));
			$(`<span class="chat-file-name"></span>`).text(file.name).appendTo($chip);
			$(`<span class="chat-file-size"></span>`).text(this.file_size(file.size)).appendTo($chip);
			$(`<button type="button" class="chat-unstage"></button>`)
				.attr({ title: __("Remove", null, CONTEXT), "aria-label": __("Remove", null, CONTEXT) })
				.append(frappe.utils.icon("x", "xs"))
				.on("click", () => {
					this.staged.splice(index, 1);
					this.render_tray();
					this.update_composer();
				})
				.appendTo($chip);
		});
	}

	voice_type() {
		if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) return null;
		return VOICE_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || null;
	}

	async start_recording() {
		const type = this.voice_type();
		if (!type || this.recorder || !this.can_send()) return;
		let stream;
		try {
			stream = await navigator.mediaDevices.getUserMedia({ audio: true });
		} catch (e) {
			frappe.show_alert({ message: __("No access to the microphone", null, CONTEXT), indicator: "red" });
			return;
		}
		const recorder = new MediaRecorder(stream, { mimeType: type });
		const chunks = [];
		const started = Date.now();
		const chat = this.chat;
		recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
		recorder.onstop = () => {
			stream.getTracks().forEach((track) => track.stop());
			if (!recorder.send || !chunks.length) return;
			const mime = type.split(";")[0];
			const ext = { "audio/ogg": "ogg", "audio/webm": "webm", "audio/mp4": "m4a" }[mime];
			const file = new File(chunks, `voice.${ext}`, { type: mime });
			this.send_files([file], "", chat, { voice: true, duration: (Date.now() - started) / 1000 });
		};
		this.recorder = recorder;
		recorder.start(1000);
		const $cancel = $(`<button type="button" class="chat-unstage"></button>`)
			.attr({ title: __("Discard", null, CONTEXT), "aria-label": __("Discard", null, CONTEXT) })
			.append(frappe.utils.icon("x", "sm"))
			.on("click", () => this.stop_recording(false));
		const $time = $(`<span class="chat-recording-time">0:00</span>`);
		this.$recording.empty().append($cancel, $(`<span class="chat-recording-dot"></span>`), $time).show();
		this.$input.hide();
		this.$attach.prop("disabled", true);
		this.recording_timer = setInterval(() => {
			const seconds = Math.floor((Date.now() - started) / 1000);
			$time.text(`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`);
			if (seconds >= VOICE_MAX_SECONDS) this.stop_recording(true);
		}, 250);
		this.update_composer();
	}

	stop_recording(send) {
		const recorder = this.recorder;
		if (!recorder) return;
		this.recorder = null;
		clearInterval(this.recording_timer);
		recorder.send = send;
		if (recorder.state !== "inactive") recorder.stop();
		this.$recording.hide().empty();
		this.$input.show();
		this.$attach.prop("disabled", false);
		this.update_composer();
		this.$input.focus();
	}

	update_composer() {
		const input = this.$input[0];
		const style = getComputedStyle(input);
		const box = ["paddingTop", "paddingBottom", "borderTopWidth", "borderBottomWidth"].reduce(
			(sum, key) => sum + (parseFloat(style[key]) || 0),
			0
		);
		const line = parseFloat(style.lineHeight) || 20;
		input.style.height = "auto";
		const borders = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
		input.style.height = `${Math.min(input.scrollHeight + borders, line * MAX_INPUT_LINES + box)}px`;
		const empty = !this.$input.val().trim() && !this.staged.length;
		const mic = empty && !this.recorder && Boolean(this.voice_type()) && this.can_send();
		this.$mic?.toggle(mic);
		this.$send.toggle(!mic);
		this.$send.prop("disabled", !this.recorder && (this.sending || empty));
	}

	chat_label(chat) {
		const same = this.chats.filter((c) => c.network_label === chat.network_label).length > 1;
		return same && chat.title ? `${chat.network_label} · ${chat.title}` : chat.network_label;
	}

	render_networks() {
		this.$network.empty();
		this.chats.forEach((chat) => $(`<option></option>`).val(chat.name).text(this.chat_label(chat)).appendTo(this.$network));
		this.$network.val(this.chat);
		this.$network.toggle(this.chats.length > 1);
		const enabled = Boolean(this.chat);
		this.$input.prop("disabled", !enabled);
		this.$input.attr(
			"placeholder",
			enabled ? __("Message via {0}", [this.selected().network_label], CONTEXT) : __("No chat to reply to", null, CONTEXT)
		);
		this.$attach?.prop("disabled", !enabled);
		this.update_composer?.();
	}

	selected() {
		return this.chats.find((c) => c.name === this.chat);
	}

	select_chat(chat) {
		this.chat = chat;
		this.render_networks();
	}

	day_of(message) {
		return message.sent_at.slice(0, 10);
	}

	sender(message) {
		const member = (this.members || []).find((m) => m.participant_id && m.participant_id === message.sender_id);
		return {
			name: member?.contact_name || message.sender_name || member?.display_name || "?",
			image: member?.avatar_url || null,
		};
	}

	meta(message, $meta) {
		if (this.chats.length > 1 && message.network_label) network_chip(message.network_label).appendTo($meta);
		if (message.direction !== "Out" && !this.is_group && this.mode === "contact" && message.sender_name && message.sender_name !== this.title)
			$(`<span class="chat-sender"></span>`).text(message.sender_name).appendTo($meta);
		$(`<span class="chat-time"></span>`).text(message.sent_at.slice(11, 16)).appendTo($meta);
		const receipt = RECEIPTS[message.receipt || (this.confirmed.has(message.name) ? "Sent" : "")];
		if (message.direction === "Out" && receipt)
			$(`<span class="chat-state"></span>`)
				.attr({ title: __(receipt.label, null, CONTEXT), "data-receipt": message.receipt || "Sent" })
				.append(frappe.utils.icon(receipt.icon, "xs"))
				.appendTo($meta);
	}

	bubble(message) {
		const out = message.direction === "Out";
		const $bubble = $(`<div class="chat-bubble"></div>`);
		const attachments = message.attachments || [];
		if (this.mode === "preview" && attachments.length)
			attachments.forEach((item) => this.attachment_name(item).appendTo($bubble));
		else if (message.kind === "Voice" && attachments[0]?.too_large) this.too_large().appendTo($bubble);
		else if (message.kind === "Voice" && message.has_media) this.voice_player(message).appendTo($bubble);
		else if (attachments.length) attachments.forEach((item, index) => this.attachment(message, item, index).appendTo($bubble));
		else if (["Voice", "Image", "File"].includes(message.kind))
			$(`<div class="chat-kind"></div>`).text(__(message.kind, null, CONTEXT)).appendTo($bubble);
		if (message.kind === "Other" && !attachments.some((item) => item.type === "img"))
			$(`<div class="chat-kind chat-unsupported"></div>`).text(unsupported_label(message.beeper_type)).appendTo($bubble);
		if (message.html) $(`<div class="chat-text chat-html"></div>`).html(message.html).appendTo($bubble);
		if (message.transcript) $(`<div class="chat-transcript"></div>`).text(message.transcript).appendTo($bubble);
		this.meta(message, $(`<div class="chat-meta"></div>`).appendTo($bubble));
		const $row = $(`<div class="chat-row"></div>`).toggleClass("out", out).attr("data-name", message.name);
		if (this.is_group && !out) {
			const sender = this.sender(message);
			$row.addClass("chat-group-row").append(avatar(sender.name, sender.image).addClass("chat-sender-avatar"));
			$(`<div class="chat-sender-name"></div>`).text(sender.name).prependTo($bubble);
		}
		$row.append($bubble);
		if (message.reactions?.length) this.reaction_chips(message).appendTo($row);
		if (this.can_react(message)) this.reaction_picker(message).appendTo($row);
		return $row;
	}

	can_react(message) {
		return ["chat", "contact"].includes(this.mode) && this.own !== false && Boolean(message.chat);
	}

	reaction_chips(message) {
		const $chips = $(`<div class="chat-reactions"></div>`);
		for (const group of message.reactions) {
			const names = group.mine ? [__("You", null, CONTEXT)] : [];
			const others = group.count - (group.mine ? 1 : 0);
			if (others > 0 && this.is_group) names.push(String(others));
			else if (others > 0) names.push(others > 1 ? `${this.title} +${others - 1}` : this.title);
			$(`<span class="chat-reaction"></span>`)
				.toggleClass("mine", group.mine)
				.toggleClass("chat-reaction-text", !is_emoji(group.key))
				.attr("title", names.join(", "))
				.text(group.count > 1 ? `${group.key} ${group.count}` : group.key)
				.appendTo($chips);
		}
		return $chips;
	}

	reaction_picker(message) {
		const $picker = $(`<div class="chat-picker" role="toolbar"></div>`).attr("aria-label", __("React", null, CONTEXT));
		const mine = (message.reactions || []).filter((group) => group.mine);
		for (const key of REACTIONS) {
			const active = mine.some((group) => group.key === key);
			$(`<button type="button" class="chat-pick"></button>`)
				.toggleClass("active", active)
				.attr({ title: key, "aria-pressed": String(active) })
				.text(key)
				.on("click", () => this.react(message, key, active))
				.appendTo($picker);
		}
		if (mine.length)
			$(`<button type="button" class="chat-pick chat-unpick"></button>`)
				.attr({ title: __("Remove my reaction", null, CONTEXT), "aria-label": __("Remove my reaction", null, CONTEXT) })
				.append(frappe.utils.icon("x", "xs"))
				.on("click", () => mine.forEach((group) => this.react(message, group.key, true)))
				.appendTo($picker);
		return $picker;
	}

	set_reactions(message, reactions) {
		message.reactions = reactions;
		this.$list.find(`.chat-row[data-name="${CSS.escape(message.name)}"]`).replaceWith(this.bubble(message));
	}

	react(message, key, remove) {
		const before = message.reactions || [];
		let after = before.map((group) => ({ ...group }));
		const own = after.find((group) => group.key === key);
		if (remove && own?.mine) {
			own.count -= 1;
			own.mine = false;
			after = after.filter((group) => group.count > 0);
		} else if (!remove && !own) {
			after.push({ key, count: 1, mine: true });
		} else if (!remove && !own.mine) {
			Object.assign(own, { count: own.count + 1, mine: true });
		}
		this.set_reactions(message, after);
		const ticket = this.ticket;
		frappe
			.call({ method: "chatsync.api.react", args: { message: message.name, key, remove: remove ? 1 : 0 } })
			.then((r) => {
				if (ticket !== this.ticket) return;
				this.set_reactions(message, r.message.reactions);
				this.reconcile(message.chat);
			})
			.catch(() => ticket === this.ticket && this.set_reactions(message, before));
	}

	reconcile(chat) {
		const ticket = this.ticket;
		clearTimeout(this.reconcile_timer);
		this.reconcile_timer = setTimeout(() => {
			frappe
				.call({ method: "chatsync.api.refresh_messages", type: "POST", silent: true, args: { chat } })
				.then((r) => ticket === this.ticket && this.merge(r.message.messages, chat))
				.catch(() => {});
		}, RECONCILE_DELAY);
	}

	media_url(message, index) {
		return `/api/method/chatsync.media.serve?message=${encodeURIComponent(message.name)}&index=${index}`;
	}

	attachment(message, item, index) {
		if (item.too_large) return this.too_large();
		const url = this.media_url(message, index);
		if (item.type === "img" && (!item.mime_type || IMAGE_TYPES.includes(item.mime_type))) {
			const $image = $(`<img class="chat-image" loading="lazy">`)
				.attr({ src: url, alt: item.file_name || "" })
				.on("load", () => this.at_bottom && this.scroll_to_bottom());
			return $(`<a class="chat-media" target="_blank" rel="noopener noreferrer"></a>`).attr("href", url).append($image);
		}
		if (item.type === "video" && VIDEO_TYPES.includes(item.mime_type)) {
			const video = document.createElement("video");
			video.className = "chat-video";
			video.controls = true;
			video.preload = "none";
			video.src = url;
			return $(`<div class="chat-media"></div>`).append(video);
		}
		const $file = $(`<a class="chat-file" download></a>`).attr("href", url);
		$file.append(frappe.utils.icon("paperclip", "sm"));
		$(`<span class="chat-file-name"></span>`).text(item.file_name || __("File", null, CONTEXT)).appendTo($file);
		if (item.file_size) $(`<span class="chat-file-size"></span>`).text(this.file_size(item.file_size)).appendTo($file);
		return $file;
	}

	attachment_name(item) {
		const $file = $(`<div class="chat-file"></div>`).append(frappe.utils.icon("paperclip", "sm"));
		$(`<span class="chat-file-name"></span>`).text(item.file_name || __("File", null, CONTEXT)).appendTo($file);
		if (item.file_size) $(`<span class="chat-file-size"></span>`).text(this.file_size(item.file_size)).appendTo($file);
		return $file;
	}

	too_large() {
		return $(`<div class="chat-kind"></div>`).text(__("File too large for the preview, please open it in Beeper"));
	}

	file_size(bytes) {
		const units = ["B", "KB", "MB", "GB"];
		let unit = 0;
		while (bytes >= 1024 && unit < units.length - 1) {
			bytes /= 1024;
			unit++;
		}
		return `${unit ? bytes.toFixed(1) : bytes} ${units[unit]}`;
	}

	voice_player(message) {
		const $voice = $(`<div class="chat-voice"></div>`);
		const audio = document.createElement("audio");
		if (!audio.canPlayType("audio/ogg; codecs=opus")) {
			$(`<span class="chat-kind"></span>`).text(__("Unsupported audio format", null, CONTEXT)).appendTo($voice);
		} else {
			audio.controls = true;
			audio.preload = "none";
			audio.src = `/api/method/chatsync.voice.play?message=${encodeURIComponent(message.name)}`;
			$voice.append(audio);
		}
		if (!message.transcript) {
			const $button = $(`<button class="btn btn-xs btn-default"></button>`)
				.text(__("Transcribe", null, CONTEXT))
				.appendTo($voice);
			$button.on("click", () => this.transcribe(message, $button));
		}
		return $voice;
	}

	transcribe(message, $button) {
		$button.prop("disabled", true).text(__("Transcribing...", null, CONTEXT));
		frappe
			.call({ method: "chatsync.voice.transcribe", type: "POST", args: { message: message.name } })
			.then((r) => {
				message.transcript = r.message;
				this.$list.find(`.chat-row[data-name="${CSS.escape(message.name)}"]`).replaceWith(this.bubble(message));
			})
			.catch(() => $button.prop("disabled", false).text(__("Transcribe", null, CONTEXT)));
	}

	render() {
		const scroll = this.$scroll[0];
		const was_bottom = this.at_bottom;
		const anchor = was_bottom ? null : this.scroll_anchor();
		this.rebuild();
		const target = anchor && this.$list.find(`.chat-row[data-name="${CSS.escape(anchor.name)}"]`)[0];
		if (target) {
			const shift = target.getBoundingClientRect().top - scroll.getBoundingClientRect().top - anchor.offset;
			scroll.scrollTop += shift;
		}
		else if (was_bottom) scroll.scrollTop = scroll.scrollHeight;
		this.at_bottom = was_bottom;
		this.rendered_top = scroll.scrollTop;
		requestAnimationFrame(() => requestAnimationFrame(() => (this.rendered_top = null)));
	}

	scroll_anchor() {
		const top = this.$scroll[0].getBoundingClientRect().top;
		const row = this.$list.find(".chat-row[data-name]").toArray().find((el) => el.getBoundingClientRect().bottom > top);
		return row && { name: row.dataset.name, offset: row.getBoundingClientRect().top - top };
	}

	user_scrolled() {
		const scroll = this.$scroll[0];
		if (scroll.scrollTop === this.rendered_top) {
			this.rendered_top = null;
			return false;
		}
		this.at_bottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
		return true;
	}

	rebuild() {
		this.$list.empty();
		let day = null;
		for (const message of this.messages) {
			if (this.day_of(message) !== day) {
				day = this.day_of(message);
				$(`<div class="chat-day"></div>`)
					.attr("data-day", day)
					.append($(`<span></span>`).text(frappe.datetime.str_to_user(day)))
					.appendTo(this.$list);
			}
			this.bubble(message).appendTo(this.$list);
		}
		if (!this.messages.length && !this.pending.length)
			$(`<div class="chat-list-empty"></div>`).text(__("No messages yet", null, CONTEXT)).appendTo(this.$list);
		this.pending.forEach((entry) => this.pending_bubble(entry).appendTo(this.$list));
		this.$more?.text(this.has_more ? __("Loading older messages...") : "");
	}

	scroll_to_bottom() {
		this.at_bottom = true;
		this.$scroll.scrollTop(this.$scroll[0].scrollHeight);
	}

	merge(incoming, chat) {
		const info = this.chats.find((c) => c.name === chat) || {};
		const known = new Set(this.messages.map((m) => m.name));
		const by_name = new Map(this.messages.map((m) => [m.name, m]));
		incoming.forEach((m) => {
			const current = by_name.get(m.name);
			if (current && !same_reactions(current.reactions, m.reactions)) current.reactions = m.reactions;
			if (current && m.receipt) current.receipt = m.receipt;
		});
		const oldest = this.has_more ? this.messages[0] : null;
		const added = incoming
			.filter((m) => !known.has(m.name) && !(oldest && compare(m, oldest) < 0))
			.map((m) => ({ ...m, chat, network: info.network, network_label: info.network_label }));
		this.messages = this.messages.concat(added).sort(compare);
		this.settle(added);
		this.render();
		return added;
	}

	newest_known(chat) {
		const own = this.messages.filter((m) => m.chat === chat);
		return own.length ? own[own.length - 1] : this.has_more ? this.messages[0] : null;
	}

	settle(added) {
		const candidates = added.filter((m) => m.direction === "Out").sort(compare).reverse();
		for (const entry of [...this.pending]) {
			const message = candidates.find(
				(m) => m.chat === entry.chat && this.is_entry(m, entry) && (!entry.after || compare(m, entry.after) > 0)
			);
			if (!message) continue;
			candidates.splice(candidates.indexOf(message), 1);
			this.pending = this.pending.filter((p) => p !== entry);
			this.confirmed.add(message.name);
		}
	}

	is_entry(message, entry) {
		if (!entry.file) return message.text === entry.text;
		return message.has_media && (!entry.text || message.text === entry.text);
	}

	/** Asks Beeper again a few times after a send, so delivered and read show up while the chat is open. */
	watch_receipts(chat) {
		const ticket = this.ticket;
		(this.receipt_timers || []).forEach((timer) => clearTimeout(timer));
		this.receipt_timers = RECEIPT_DELAYS.map((delay) =>
			setTimeout(() => {
				if (ticket !== this.ticket || document.hidden) return;
				frappe
					.call({ method: "chatsync.api.refresh_messages", type: "POST", silent: true, args: { chat } })
					.then((r) => ticket === this.ticket && this.merge(r.message.messages, chat))
					.catch(() => {});
			}, delay)
		);
	}

	pending_bubble(entry) {
		const info = this.chats.find((c) => c.name === entry.chat) || {};
		const $meta = $(`<div class="chat-meta"></div>`);
		if (this.chats.length > 1 && info.network_label) network_chip(info.network_label).appendTo($meta);
		$(`<span class="chat-time"></span>`).text(entry.time).appendTo($meta);
		$(`<span class="chat-state"></span>`)
			.attr("title", __("Sending...", null, CONTEXT))
			.append(frappe.utils.icon("clock", "xs"))
			.appendTo($meta);
		const $bubble = $(`<div class="chat-bubble"></div>`);
		if (entry.voice) $(`<div class="chat-kind"></div>`).text(`🎤 ${__("Voice message", null, CONTEXT)}`).appendTo($bubble);
		else if (entry.file) this.attachment_name({ file_name: entry.file, file_size: entry.size }).appendTo($bubble);
		if (entry.text) $(`<div class="chat-text"></div>`).text(entry.text).appendTo($bubble);
		return $(`<div class="chat-row out chat-pending"></div>`).append($bubble.append($meta));
	}

	clear_pending() {
		clearTimeout(this.reconcile_timer);
		(this.receipt_timers || []).forEach((timer) => clearTimeout(timer));
		this.stop_recording(false);
		this.staged = [];
		if (this.$tray) this.render_tray();
		this.pending = [];
		this.timers.forEach((timer) => clearTimeout(timer));
		this.timers.clear();
	}

	poll_sent(entry, attempt = 0) {
		const ticket = this.ticket;
		const timer = setTimeout(async () => {
			this.timers.delete(timer);
			if (ticket !== this.ticket || !this.pending.includes(entry)) return;
			let r;
			try {
				r = await frappe.call({
					method: "chatsync.api.refresh_messages",
					type: "POST",
					silent: true,
					args: { chat: entry.chat },
				});
			} catch (e) {
				return this.give_up(entry, ticket);
			}
			if (ticket !== this.ticket) return;
			this.merge(r.message.messages, entry.chat);
			if (!this.pending.includes(entry)) {
				if (this.at_bottom) this.scroll_to_bottom();
				this.watch_receipts(entry.chat);
			} else if (attempt + 1 < POLL_DELAYS.length) {
				this.poll_sent(entry, attempt + 1);
			} else {
				this.give_up(entry, ticket);
			}
		}, POLL_DELAYS[attempt]);
		this.timers.add(timer);
	}

	give_up(entry, ticket) {
		if (ticket !== this.ticket || !this.pending.includes(entry)) return;
		this.pending = this.pending.filter((p) => p !== entry);
		this.render();
		frappe.show_alert({
			message: __("Not confirmed yet, it will appear at the next sync", null, CONTEXT),
			indicator: "orange",
		});
	}

	send() {
		const text = this.$input.val().trim();
		const chat = this.chat;
		if (this.staged.length && !this.sending && chat) {
			const files = this.staged;
			this.staged = [];
			this.render_tray();
			this.$input.val("");
			return this.send_files(files, text, chat);
		}
		if (!text || this.sending || !chat) return;
		const ticket = this.ticket;
		const entry = { chat, text, time: now_time(), after: this.newest_known(chat) };
		this.sending = true;
		this.pending.push(entry);
		this.$input.val("");
		this.render();
		this.scroll_to_bottom();
		this.update_composer();
		frappe
			.call({ method: "chatsync.api.send_message", args: { chat, text } })
			.then(() => {
				if (ticket !== this.ticket) return;
				this.poll_sent(entry);
				this.after_send();
			})
			.catch(() => {
				if (ticket !== this.ticket) return;
				this.pending = this.pending.filter((p) => p !== entry);
				this.render();
				if (!this.$input.val()) this.$input.val(text);
			})
			.always(() => {
				this.sending = false;
				if (ticket === this.ticket) this.$input.focus();
				this.update_composer();
			});
	}

	/** One message per file; the caption goes with the first. Uploads run one after the other. */
	async send_files(files, text, chat, { voice = false, duration = null } = {}) {
		const ticket = this.ticket;
		const entries = files.map((file, index) => ({
			chat,
			file: file.name,
			size: file.size,
			voice,
			text: index === 0 ? text : "",
			time: now_time(),
			after: this.newest_known(chat),
		}));
		this.sending = true;
		this.pending.push(...entries);
		this.render();
		this.scroll_to_bottom();
		this.update_composer();
		for (const [index, file] of files.entries()) {
			const entry = entries[index];
			const form = new FormData();
			form.append("chat", chat);
			form.append("file", file, file.name);
			if (entry.text) form.append("text", entry.text);
			if (voice) {
				form.append("voice_note", "1");
				if (duration) form.append("duration", String(duration));
			}
			try {
				const response = await fetch("/api/method/chatsync.api.send_file", {
					method: "POST",
					body: form,
					headers: { "X-Frappe-CSRF-Token": frappe.csrf_token, Accept: "application/json" },
				});
				if (!response.ok) throw await response.json().catch(() => ({}));
				if (ticket !== this.ticket) return;
				this.poll_sent(entry);
			} catch (r) {
				if (ticket !== this.ticket) return;
				this.pending = this.pending.filter((p) => p !== entry);
				this.render();
				frappe.show_alert({
					message: server_message(r) || __("{0} could not be sent", [file.name], CONTEXT),
					indicator: "red",
				});
			}
		}
		this.sending = false;
		if (ticket === this.ticket) {
			this.after_send();
			this.update_composer();
			this.$input.focus();
		}
	}

	after_send() {}
}

class QuickReply extends ChatPane {
	constructor(contact, select) {
		super();
		this.init_pane();
		this.contact = contact;
		this.mode = "contact";
		this.dialog = new frappe.ui.Dialog({ title: __("Chat", null, "Contact action"), size: "large" });
		this.dialog.$wrapper.addClass("chatsync-quick-dialog");
		const $root = $(`<div class="chatsync-ui chatsync-quick"></div>`).appendTo(this.dialog.$body.empty());
		const $header = $(`<header class="chat-header"></header>`).appendTo($root);
		this.$avatar = $(`<div></div>`).appendTo($header);
		const $titles = $(`<div class="chat-titles"></div>`).appendTo($header);
		this.$title = $(`<div class="chat-title"></div>`).appendTo($titles);
		this.$subtitle = $(`<div class="chat-subtitle"></div>`).appendTo($titles);
		const $tools = $(`<div class="chat-quick-tools"></div>`).appendTo($header);
		$(`<a class="chat-open-full"></a>`)
			.attr("href", `/desk/chat-view?contact=${encodeURIComponent(contact)}`)
			.text(__("Open in chat", null, CONTEXT))
			.on("click", (e) => {
				if (e.metaKey || e.ctrlKey || e.shiftKey) return;
				e.preventDefault();
				this.dialog.hide();
				frappe.set_route("chat-view", { contact, select: this.chat });
			})
			.appendTo($tools);
		this.$scroll = $(`<div class="chat-scroll"></div>`).appendTo($root);
		this.$list = $(`<div class="chat-messages"></div>`).appendTo(this.$scroll);
		this.$list.append($(`<div class="chat-list-empty"></div>`).text(__("Loading...", null, CONTEXT)));
		this.$scroll.on("scroll", () => this.user_scrolled());
		this.make_composer($root, $tools);
		this.$network.hide();
		this.$input.prop("disabled", true);
		this.dialog.onhide = () => {
			this.ticket++;
			this.clear_pending();
		};
		this.dialog.$wrapper.on("hidden.bs.modal", () => this.dialog.$wrapper.remove());
		this.dialog.show();
		this.load(select);
	}

	load(select) {
		const ticket = this.ticket;
		frappe.call({
			method: "chatsync.api.get_contact_messages",
			type: "GET",
			silent: true,
			args: { contact: this.contact, limit: 10 },
			callback: (r) => {
				if (ticket !== this.ticket) return;
				const data = r.message;
				this.chats = data.chats;
				this.chat = data.chats.some((c) => c.name === select) ? select : data.default_chat;
				this.title = data.contact_name || this.contact;
				this.messages = data.messages;
				this.has_more = data.has_more;
				this.$avatar.empty().append(avatar(this.title, data.avatar_url));
				this.$title.text(this.title);
				const $subtitle = this.$subtitle.empty();
				[...new Set(this.chats.map((c) => c.network_label))].forEach((label) => network_chip(label).appendTo($subtitle));
				this.render_networks();
				this.render();
				this.scroll_to_bottom();
				this.update_composer();
				this.$input.focus();
			},
			error: (r) => {
				if (ticket !== this.ticket) return;
				const text = server_message(r) || __("The chat could not be loaded.", null, CONTEXT);
				this.$list.empty().append($(`<div class="chat-list-empty chat-list-error"></div>`).text(text));
			},
		});
	}
}

Object.assign(chatsync.ui, {
	CONTEXT,
	ChatPane,
	assign_group,
	group_icon,
	group_summary,
	QuickReply,
	avatar,
	network_chip,
	plain_text,
	server_message,
	local_time,
	quick_reply: (contact, select) => new QuickReply(contact, select),
});
