const CONTEXT = "Chat View";

frappe.pages["chat-view"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Chat"), single_column: true });
	wrapper.chat_view = new ChatView(page);
};

frappe.pages["chat-view"].on_page_show = function (wrapper) {
	const [, chat] = frappe.get_route();
	const options = frappe.route_options || {};
	frappe.route_options = null;
	const day = options.day || new URLSearchParams(window.location.search).get("day");
	if (chat) wrapper.chat_view.open(chat, day);
};

class ChatView {
	constructor(page) {
		this.page = page;
		this.$body = $(`<div class="chat-view"></div>`).appendTo(page.body);
		this.$scroll = $(`<div class="chat-scroll"></div>`).appendTo(this.$body);
		this.$more = $(`<div class="chat-more"></div>`).appendTo(this.$scroll);
		this.$list = $(`<div></div>`).appendTo(this.$scroll);
		this.$input = $(`<textarea class="form-control"></textarea>`);
		$(`<div class="chat-reply"></div>`).append(this.$input).appendTo(this.$body);
		this.$scroll.on("scroll", () => this.$scroll.scrollTop() < 40 && this.load_older());
		this.$input.on("keydown", (e) => {
			if (e.key === "Enter" && !e.shiftKey && !e.originalEvent.isComposing) {
				e.preventDefault();
				this.send();
			}
		});
	}

	open(chat, day) {
		this.chat = chat;
		this.messages = [];
		this.has_more = false;
		this.loading = null;
		this.clear_pending();
		this.$list.empty();
		this.page.clear_menu();
		frappe
			.call({ method: "chatsync.api.get_messages", args: { chat } })
			.then(async (r) => {
				const data = r.message;
				this.data = data;
				this.has_more = data.has_more;
				this.messages = data.messages;
				this.page.set_title(data.chat.contact_name || data.chat.title || chat);
				this.page.set_indicator(frappe.utils.escape_html(data.chat.network || ""), "gray");
				this.add_actions(data);
				this.render();
				this.$input.attr("placeholder", __("Reply", null, CONTEXT));
				if (day) await this.reveal(day);
				else this.scroll_to_bottom();
			});
	}

	add_actions(data) {
		this.page.add_menu_item(__("Reassign to another contact", null, CONTEXT), () => this.reassign());
		this.page.add_menu_item(__("Unassign", null, CONTEXT), () => this.unassign());
		if (data.archive_available) {
			this.page.add_menu_item(__("Archive", null, CONTEXT), () => this.archive());
		}
	}

	reassign() {
		const dialog = new frappe.ui.Dialog({
			title: __("Reassign to another contact", null, CONTEXT),
			fields: [{ fieldname: "contact", fieldtype: "Link", options: "Contact", label: __("Contact"), reqd: 1 }],
			primary_action_label: __("Reassign", null, CONTEXT),
			primary_action: ({ contact }) => {
				frappe
					.call({ method: "chatsync.api.reassign_chat", args: { chat: this.chat, contact } })
					.then((r) => {
						dialog.hide();
						this.forget_contacts(contact);
						frappe.set_route("chat-view", r.message);
					});
			},
		});
		dialog.show();
	}

	archive() {
		const dialog = new frappe.ui.Dialog({
			title: __("Archive", null, CONTEXT),
			fields: [
				{ fieldname: "date_from", fieldtype: "Date", label: __("From", null, CONTEXT) },
				{ fieldname: "date_to", fieldtype: "Date", label: __("To", null, CONTEXT) },
				{ fieldtype: "HTML", options: `<p class="text-muted">${__("Leave empty to archive the whole chat", null, CONTEXT)}</p>` },
			],
			primary_action_label: __("Archive", null, CONTEXT),
			primary_action: ({ date_from, date_to }) => {
				frappe
					.call({ method: "chatsync.archive.archive_chat", type: "POST", args: { chat: this.chat, date_from, date_to } })
					.then((r) => {
						dialog.hide();
						const url = frappe.utils.get_form_link("Business Document", r.message);
						frappe.msgprint({
							title: __("Archive", null, CONTEXT),
							indicator: "green",
							message: `${__("Archived", null, CONTEXT)}: <a href="${url}">${frappe.utils.escape_html(r.message)}</a>`,
						});
					});
			},
		});
		dialog.show();
	}

	unassign() {
		frappe.confirm(__("Unassign this chat from the contact? Its messages will be deleted."), () =>
			frappe
				.call({ method: "chatsync.api.unassign_chat", args: { chat: this.chat } })
				.then(() => {
					this.forget_contacts();
					frappe.set_route(this.data.chat.contact ? ["Form", "Contact", this.data.chat.contact] : ["List", "Chat"]);
				})
		);
	}

	forget_contacts(...extra) {
		[this.data.chat.contact, ...extra].filter(Boolean).forEach((contact) => frappe.model.remove_from_locals("Contact", contact));
	}

	day_of(message) {
		return message.sent_at.slice(0, 10);
	}

	bubble(message) {
		const out = message.direction === "Out";
		const $bubble = $(`<div class="chat-bubble"></div>`);
		if (message.kind === "Voice" && message.has_media) this.voice_player(message).appendTo($bubble);
		else if (message.kind !== "Text") $(`<div class="chat-kind"></div>`).text(__(message.kind, null, CONTEXT)).appendTo($bubble);
		if (message.html) $(`<div class="chat-text chat-html"></div>`).html(message.html).appendTo($bubble);
		if (message.transcript) $(`<div class="chat-transcript"></div>`).text(message.transcript).appendTo($bubble);
		const sender = out ? "" : message.sender_name ? `${message.sender_name} · ` : "";
		$(`<div class="chat-meta"></div>`).text(sender + message.sent_at.slice(11, 16)).appendTo($bubble);
		return $(`<div class="chat-row"></div>`)
			.toggleClass("out", out)
			.attr("data-name", message.name)
			.append($bubble);
	}

	voice_player(message) {
		const $voice = $(`<div class="chat-voice"></div>`);
		$(`<span class="chat-kind"></span>`).text("🎤").appendTo($voice);
		const audio = document.createElement("audio");
		if (!audio.canPlayType('audio/ogg; codecs=opus')) {
			$(`<span class="text-muted"></span>`).text(__("Unsupported audio format", null, CONTEXT)).appendTo($voice);
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
				this.$list
					.find(`.chat-row[data-name="${CSS.escape(message.name)}"]`)
					.replaceWith(this.bubble(message));
			})
			.catch(() => $button.prop("disabled", false).text(__("Transcribe", null, CONTEXT)));
	}

	render() {
		this.$list.empty();
		let day = null;
		for (const message of this.messages) {
			if (this.day_of(message) !== day) {
				day = this.day_of(message);
				$(`<div class="chat-day"></div>`)
					.attr("data-day", day)
					.text(frappe.datetime.str_to_user(day))
					.appendTo(this.$list);
			}
			this.bubble(message).appendTo(this.$list);
		}
		this.$pending?.appendTo(this.$list);
		this.$more.text(this.has_more ? __("Loading older messages...") : "");
	}

	load_older() {
		if (!this.has_more || !this.messages.length) return Promise.resolve(false);
		this.loading ||= this.fetch_older().finally(() => (this.loading = null));
		return this.loading;
	}

	async fetch_older() {
		const r = await frappe.call({
			method: "chatsync.api.get_messages",
			args: { chat: this.chat, before: this.messages[0].name },
		});
		const height = this.$scroll[0].scrollHeight;
		this.messages = r.message.messages.concat(this.messages);
		this.has_more = r.message.has_more;
		this.render();
		this.$scroll.scrollTop(this.$scroll[0].scrollHeight - height);
		return true;
	}

	async reveal(day) {
		while (this.messages.length && this.day_of(this.messages[0]) > day && this.has_more) {
			if (!(await this.load_older())) break;
		}
		const target = this.$list.find(".chat-day").filter((_, el) => $(el).attr("data-day") >= day)[0];
		if (!target) return this.scroll_to_bottom();
		this.$scroll.scrollTop(target.offsetTop - this.$scroll[0].offsetTop);
	}

	scroll_to_bottom() {
		this.$scroll.scrollTop(this.$scroll[0].scrollHeight);
	}

	merge(incoming) {
		const known = new Set(this.messages.map((m) => m.name));
		const added = incoming.filter((m) => !known.has(m.name));
		this.messages = this.messages.concat(added).sort((a, b) => a.sent_at.localeCompare(b.sent_at));
		this.render();
		return added;
	}

	show_pending(text) {
		this.clear_pending();
		this.$pending = $(`<div class="chat-row out chat-pending"></div>`).append(
			$(`<div class="chat-bubble"></div>`)
				.append($(`<div class="chat-text"></div>`).text(text))
				.append($(`<div class="chat-meta"></div>`).text(__("Sending...", null, CONTEXT)))
		);
		this.$pending.appendTo(this.$list);
		this.scroll_to_bottom();
	}

	clear_pending() {
		this.$pending?.remove();
		this.$pending = null;
		clearTimeout(this.poll_timer);
	}

	poll_sent(text, tries = 4) {
		const chat = this.chat;
		this.poll_timer = setTimeout(async () => {
			if (!this.$pending) return;
			let r;
			try {
				r = await frappe.call({ method: "chatsync.api.refresh_messages", type: "POST", args: { chat } });
			} catch (e) {
				return this.give_up(chat);
			}
			if (chat !== this.chat) return;
			const added = this.merge(r.message.messages);
			if (added.some((m) => m.direction === "Out" && m.text === text)) {
				this.clear_pending();
				this.scroll_to_bottom();
			} else if (tries > 1) {
				this.poll_sent(text, tries - 1);
			} else {
				this.give_up(chat);
			}
		}, 2500);
	}

	give_up(chat) {
		if (chat !== this.chat) return;
		this.clear_pending();
		frappe.show_alert({
			message: __("Not confirmed yet, it will appear at the next sync", null, CONTEXT),
			indicator: "orange",
		});
	}

	send() {
		const text = this.$input.val().trim();
		if (!text || this.sending) return;
		this.sending = true;
		this.$input.prop("disabled", true);
		frappe
			.call({ method: "chatsync.api.send_message", args: { chat: this.chat, text } })
			.then((r) => {
				this.$input.val("");
				const added = this.merge(r.message.messages);
				if (added.some((m) => m.direction === "Out" && m.text === text)) {
					this.scroll_to_bottom();
				} else {
					this.show_pending(text);
					this.poll_sent(text);
				}
			})
			.always(() => {
				this.sending = false;
				this.$input.prop("disabled", false).focus();
			});
	}
}
