frappe.provide("chatsync.ui");

const CONTEXT = "Chat View";
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];
const VIDEO_TYPES = ["video/mp4", "video/webm"];
const MAX_INPUT_LINES = 6;
const POLL_DELAYS = [500, 1000, 2000, 4000, 8000];
const REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
const RECONCILE_DELAY = 2000;

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
	}

	make_composer($parent, $network_parent) {
		const $composer = $(`<footer class="chat-composer"></footer>`).appendTo($parent);
		this.$network = $(`<select class="form-control chat-network"></select>`)
			.attr({ "aria-label": __("Send via", null, CONTEXT), title: __("Send via", null, CONTEXT) })
			.on("change", () => this.select_chat(this.$network.val()))
			.appendTo($network_parent || $composer);
		this.$input = $(`<textarea class="form-control chat-input" rows="1"></textarea>`).appendTo($composer);
		this.$send = $(`<button type="button" class="btn btn-primary chat-send" disabled></button>`)
			.attr({ title: __("Send", null, CONTEXT), "aria-label": __("Send", null, CONTEXT) })
			.append(frappe.utils.icon("send-horizontal", "sm"))
			.on("click", () => this.send())
			.appendTo($composer);
		this.$input.on("input", () => this.update_composer());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey && !e.originalEvent.isComposing) {
				e.preventDefault();
				this.send();
			}
		});
		return $composer;
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
		this.$send.prop("disabled", this.sending || !this.$input.val().trim());
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

	meta(message, $meta) {
		if (this.chats.length > 1 && message.network_label) network_chip(message.network_label).appendTo($meta);
		if (message.direction !== "Out" && this.mode === "contact" && message.sender_name && message.sender_name !== this.title)
			$(`<span class="chat-sender"></span>`).text(message.sender_name).appendTo($meta);
		$(`<span class="chat-time"></span>`).text(message.sent_at.slice(11, 16)).appendTo($meta);
		if (this.confirmed.has(message.name))
			$(`<span class="chat-state"></span>`)
				.attr("title", __("Sent", null, CONTEXT))
				.append(frappe.utils.icon("check", "xs"))
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
		const $row = $(`<div class="chat-row"></div>`).toggleClass("out", out).attr("data-name", message.name).append($bubble);
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
			if (others > 0) names.push(others > 1 ? `${this.title} +${others - 1}` : this.title);
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
				(m) => m.chat === entry.chat && m.text === entry.text && (!entry.after || compare(m, entry.after) > 0)
			);
			if (!message) continue;
			candidates.splice(candidates.indexOf(message), 1);
			this.pending = this.pending.filter((p) => p !== entry);
			this.confirmed.add(message.name);
		}
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
		return $(`<div class="chat-row out chat-pending"></div>`).append(
			$(`<div class="chat-bubble"></div>`).append($(`<div class="chat-text"></div>`).text(entry.text)).append($meta)
		);
	}

	clear_pending() {
		clearTimeout(this.reconcile_timer);
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
	QuickReply,
	avatar,
	network_chip,
	plain_text,
	server_message,
	local_time,
	quick_reply: (contact, select) => new QuickReply(contact, select),
});
