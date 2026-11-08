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
		this.loading = false;
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
				this.page.set_indicator(data.chat.network || "", "gray");
				this.add_actions(data);
				this.render();
				this.$input.attr("placeholder", __("Reply"));
				if (day) await this.reveal(day);
				else this.scroll_to_bottom();
			});
	}

	add_actions(data) {
		this.page.add_menu_item(__("Reassign to another contact"), () => this.reassign());
		this.page.add_menu_item(__("Unassign"), () => this.unassign());
		if (data.archive_available) {
			this.page.add_menu_item(__("Archive"), () =>
				$(document).trigger("chatsync:archive", [this.chat])
			);
		}
	}

	reassign() {
		const dialog = new frappe.ui.Dialog({
			title: __("Reassign to another contact"),
			fields: [{ fieldname: "contact", fieldtype: "Link", options: "Contact", label: __("Contact"), reqd: 1 }],
			primary_action_label: __("Reassign"),
			primary_action: ({ contact }) => {
				frappe
					.call({ method: "chatsync.api.reassign_chat", args: { chat: this.chat, contact } })
					.then(() => {
						dialog.hide();
						this.open(this.chat);
					});
			},
		});
		dialog.show();
	}

	unassign() {
		frappe.confirm(__("Unassign this chat from the contact? Its messages will be deleted."), () =>
			frappe
				.call({ method: "chatsync.api.unassign_chat", args: { chat: this.chat } })
				.then(() => frappe.set_route(this.data.chat.contact ? ["Form", "Contact", this.data.chat.contact] : ["List", "Chat"]))
		);
	}

	day_of(message) {
		return message.sent_at.slice(0, 10);
	}

	bubble(message) {
		const out = message.direction === "Out";
		const $bubble = $(`<div class="chat-bubble"></div>`);
		if (message.kind !== "Text") $(`<div class="chat-kind"></div>`).text(__(message.kind)).appendTo($bubble);
		if (message.text) $(`<div class="chat-text"></div>`).text(message.text).appendTo($bubble);
		if (message.transcript) $(`<div class="chat-transcript"></div>`).text(message.transcript).appendTo($bubble);
		const sender = out ? "" : message.sender_name ? `${message.sender_name} · ` : "";
		$(`<div class="chat-meta"></div>`).text(sender + message.sent_at.slice(11, 16)).appendTo($bubble);
		return $(`<div class="chat-row"></div>`)
			.toggleClass("out", out)
			.attr("data-name", message.name)
			.append($bubble);
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
		this.$more.text(this.has_more ? __("Loading older messages...") : "");
	}

	async load_older() {
		if (this.loading || !this.has_more || !this.messages.length) return false;
		this.loading = true;
		const r = await frappe.call({
			method: "chatsync.api.get_messages",
			args: { chat: this.chat, before: this.messages[0].name },
		});
		const height = this.$scroll[0].scrollHeight;
		this.messages = r.message.messages.concat(this.messages);
		this.has_more = r.message.has_more;
		this.render();
		this.$scroll.scrollTop(this.$scroll[0].scrollHeight - height);
		this.loading = false;
		return true;
	}

	async reveal(day) {
		while (this.messages.length && this.day_of(this.messages[0]) > day && this.has_more) {
			await this.load_older();
		}
		const target = this.$list.find(".chat-day").filter((_, el) => $(el).attr("data-day") >= day)[0];
		if (!target) return this.scroll_to_bottom();
		this.$scroll.scrollTop(target.offsetTop - this.$scroll[0].offsetTop);
	}

	scroll_to_bottom() {
		this.$scroll.scrollTop(this.$scroll[0].scrollHeight);
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
				const known = new Set(this.messages.map((m) => m.name));
				const added = r.message.messages.filter((m) => !known.has(m.name));
				this.messages = this.messages.concat(added).sort((a, b) => a.sent_at.localeCompare(b.sent_at));
				this.render();
				this.scroll_to_bottom();
			})
			.always(() => {
				this.sending = false;
				this.$input.prop("disabled", false).focus();
			});
	}
}
