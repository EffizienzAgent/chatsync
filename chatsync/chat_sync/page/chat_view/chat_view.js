const CONTEXT = "Chat View";
const IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];
const VIDEO_TYPES = ["video/mp4", "video/webm"];
const MAX_INPUT_LINES = 6;
const SEARCH_DELAY = 400;

frappe.pages["chat-view"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Conversations", null, CONTEXT), single_column: true });
	wrapper.chat_view = new ChatView(page);
};

frappe.pages["chat-view"].on_page_show = function (wrapper) {
	const [, chat] = frappe.get_route();
	const options = frappe.route_options || {};
	frappe.route_options = null;
	const params = new URLSearchParams(window.location.search);
	wrapper.chat_view.show({
		chat,
		contact: options.contact || params.get("contact"),
		day: options.day || params.get("day"),
		select: options.select || params.get("select"),
	});
};

function avatar(title) {
	const [bg, fg] = frappe.get_palette(title || "?");
	return $(`<div class="chat-avatar"></div>`)
		.css({ background: `var(${bg})`, color: `var(${fg})` })
		.text(frappe.get_abbr(title || "?"));
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

class ChatView {
	constructor(page) {
		this.page = page;
		this.page.wrapper.addClass("chat-view-page");
		this.$body = $(`<div class="chat-view"></div>`).appendTo(page.body);
		this.conversations = [];
		this.chats = [];
		this.messages = [];
		this.ticket = 0;
		this.segment = "conversations";
		this.unassigned = { items: [], cursor: null, loaded: false, loading: false, error: null, ticket: 0 };
		this.make_list_pane();
		this.make_conversation_pane();
	}

	make_list_pane() {
		const $pane = $(`<aside class="chat-list-pane"></aside>`).appendTo(this.$body);
		const $segments = $(`<div class="chat-segments" role="tablist"></div>`).appendTo($pane);
		this.$segments = {
			conversations: __("Conversations", null, CONTEXT),
			unassigned: __("Unassigned", null, CONTEXT),
		};
		for (const [name, label] of Object.entries(this.$segments)) {
			this.$segments[name] = $(`<button type="button" class="chat-segment" role="tab"></button>`)
				.text(label)
				.on("click", () => this.set_segment(name))
				.appendTo($segments);
		}
		const $search = $(`<label class="chat-search"></label>`).appendTo($pane);
		$search.append(frappe.utils.icon("search", "sm"));
		this.$search = $(`<input type="search" class="form-control">`)
			.attr({ placeholder: __("Search", null, CONTEXT), "aria-label": __("Search", null, CONTEXT) })
			.appendTo($search)
			.on("input", () => this.search());
		const $filter = $(`<label class="chat-show-ignored"></label>`).appendTo($pane);
		this.$show_ignored = $(`<input type="checkbox">`)
			.on("change", () => this.load_unassigned())
			.appendTo($filter);
		$(`<span></span>`).text(__("Show ignored", null, CONTEXT)).appendTo($filter);
		this.$filter = $filter.hide();
		this.$conversations = $(`<div class="chat-conversations" role="list"></div>`).appendTo($pane);
		this.$unassigned = $(`<div class="chat-conversations chat-unassigned" role="list"></div>`).appendTo($pane).hide();
		this.mark_segment();
	}

	set_segment(name) {
		if (this.segment === name) return;
		this.segment = name;
		this.$search.val("");
		this.mark_segment();
		if (name === "unassigned") this.load_unassigned();
		else this.render_conversations();
	}

	mark_segment() {
		const unassigned = this.segment === "unassigned";
		Object.entries(this.$segments).forEach(([name, $tab]) =>
			$tab.toggleClass("active", name === this.segment).attr("aria-selected", String(name === this.segment))
		);
		this.$conversations.toggle(!unassigned);
		this.$unassigned.toggle(unassigned);
		this.$filter.toggle(unassigned);
		const placeholder = unassigned ? __("Search in Beeper", null, CONTEXT) : __("Search", null, CONTEXT);
		this.$search.attr({ placeholder, "aria-label": placeholder });
	}

	search() {
		if (this.segment !== "unassigned") return this.render_conversations();
		clearTimeout(this.search_timer);
		this.search_timer = setTimeout(() => this.load_unassigned(), SEARCH_DELAY);
	}

	load_unassigned(more = false) {
		const state = this.unassigned;
		const ticket = ++state.ticket;
		const query = (this.$search.val() || "").trim();
		if (!more) Object.assign(state, { items: [], cursor: null });
		Object.assign(state, { loading: true, error: null, query });
		this.render_unassigned();
		frappe.call({
			method: "chatsync.api.unassigned_page",
			type: "GET",
			silent: true,
			args: {
				cursor: more ? state.cursor : null,
				query: query || null,
				include_ignored: this.$show_ignored.prop("checked") ? 1 : 0,
			},
			callback: (r) => {
				if (ticket !== state.ticket) return;
				const known = new Set(state.items.map((item) => item.beeper_chat_id));
				state.items = state.items.concat(r.message.items.filter((item) => !known.has(item.beeper_chat_id)));
				Object.assign(state, { cursor: r.message.next_cursor, loading: false, loaded: true });
				this.render_unassigned();
			},
			error: (r) => {
				if (ticket !== state.ticket) return;
				Object.assign(state, {
					loading: false,
					error: server_message(r) || __("The chats could not be loaded from Beeper.", null, CONTEXT),
				});
				this.render_unassigned();
			},
		});
	}

	render_unassigned() {
		const state = this.unassigned;
		const $list = this.$unassigned.empty();
		state.items.forEach((item) => this.unassigned_row(item).appendTo($list));
		const $footer = $(`<div class="chat-list-empty"></div>`);
		if (state.loading) {
			$footer.text(__("Loading...", null, CONTEXT));
		} else if (state.error) {
			$footer.addClass("chat-list-error").append($(`<div></div>`).text(state.error));
			$(`<button type="button" class="btn btn-xs btn-default"></button>`)
				.text(__("Try again", null, CONTEXT))
				.on("click", () => this.load_unassigned(Boolean(state.items.length)))
				.appendTo($footer);
		} else if (state.cursor) {
			$(`<button type="button" class="btn btn-xs btn-default"></button>`)
				.text(__("Load more", null, CONTEXT))
				.on("click", () => this.load_unassigned(true))
				.appendTo($footer);
		} else if (!state.items.length) {
			$footer.text(state.query ? __("No matches", null, CONTEXT) : __("No unassigned chats", null, CONTEXT));
		} else {
			$footer.empty();
		}
		if (!$footer.is(":empty")) $footer.appendTo($list);
		this.mark_active();
	}

	unassigned_title(item) {
		return item.title || item.username || item.phone || item.beeper_chat_id;
	}

	unassigned_row(item) {
		const title = this.unassigned_title(item);
		const $row = $(`<div class="chat-row-item chat-unassigned-item" role="listitem" tabindex="0"></div>`)
			.attr("data-key", `unassigned:${item.beeper_chat_id}`)
			.toggleClass("chat-ignored", Boolean(item.ignored));
		$row.append(avatar(title));
		const $text = $(`<div class="chat-row-text"></div>`).appendTo($row);
		const $top = $(`<div class="chat-row-top"></div>`).appendTo($text);
		$(`<span class="chat-row-title"></span>`).text(title).appendTo($top);
		$(`<span class="chat-row-time"></span>`).text(this.short_time(local_time(item.last_activity))).appendTo($top);
		const $bottom = $(`<div class="chat-row-bottom"></div>`).appendTo($text);
		$(`<span class="chat-row-preview"></span>`).text(item.phone || item.username || "").appendTo($bottom);
		const $networks = $(`<span class="chat-row-networks"></span>`).appendTo($bottom);
		network_chip(item.network_label).appendTo($networks);
		const $badges = $(`<div class="chat-row-badges"></div>`);
		const badge = (text, kind) => $(`<span class="chat-badge"></span>`).addClass(kind || "").text(text).appendTo($badges);
		if (item.ignored) badge(__("Ignored", null, CONTEXT), "muted");
		if (item.ambiguous) badge(__("Unclear", null, CONTEXT));
		if (item.existing_contact) badge(__("Contact exists", null, CONTEXT));
		if (item.suggested_contact) {
			badge(__("Suggestion: {0}", [item.suggested_contact_name], CONTEXT), "suggestion");
			$(`<button type="button" class="btn btn-xs btn-primary chat-suggestion-assign"></button>`)
				.text(__("Assign", null, CONTEXT))
				.on("click", (e) => {
					e.stopPropagation();
					this.assign_unassigned(item, item.suggested_contact, $(e.currentTarget));
				})
				.appendTo($badges);
		}
		if ($badges.children().length) $badges.appendTo($text);
		$row.on("click", () => this.open_preview(item));
		$row.on("keydown", (e) => {
			if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
				e.preventDefault();
				this.open_preview(item);
			}
		});
		return $row;
	}

	open_preview(item) {
		const ticket = ++this.ticket;
		this.clear_pending();
		this.page.clear_menu();
		this.mode = "preview";
		this.preview = item;
		this.key = `unassigned:${item.beeper_chat_id}`;
		this.contact = null;
		this.chat = null;
		this.chats = [{ name: null, network: item.network, network_label: item.network_label, title: item.title }];
		this.title = this.unassigned_title(item);
		this.messages = [];
		this.has_more = false;
		this.$body.addClass("chat-open");
		this.$empty.hide();
		this.$conversation.addClass("chat-previewing").show();
		this.render_header();
		this.render_preview_actions();
		this.$more.text("");
		this.$list.empty().append($(`<div class="chat-list-empty"></div>`).text(__("Loading...", null, CONTEXT)));
		this.mark_active();
		frappe.call({
			method: "chatsync.api.preview_chat",
			type: "GET",
			silent: true,
			args: { beeper_chat_id: item.beeper_chat_id },
			callback: (r) => {
				if (ticket !== this.ticket) return;
				this.messages = r.message.messages;
				this.render();
				this.scroll_to_bottom();
			},
			error: (r) => {
				if (ticket !== this.ticket) return;
				const text = server_message(r) || __("The chat could not be loaded.", null, CONTEXT);
				this.$list.empty().append($(`<div class="chat-list-empty chat-list-error"></div>`).text(text));
			},
		});
	}

	render_preview_actions() {
		const item = this.preview;
		const $bar = this.$actions.empty().show();
		$(`<div class="chat-preview-note"></div>`)
			.text(__("Preview: nothing is stored until you assign the chat", null, CONTEXT))
			.appendTo($bar);
		const $buttons = $(`<div class="chat-preview-buttons"></div>`).appendTo($bar);
		const button = (label, action, primary) =>
			$(`<button type="button" class="btn btn-sm"></button>`)
				.addClass(primary ? "btn-primary" : "btn-default")
				.text(label)
				.on("click", (e) => action($(e.currentTarget)))
				.appendTo($buttons);
		if (item.suggested_contact)
			button(
				__("Assign to {0}", [item.suggested_contact_name], CONTEXT),
				($button) => this.assign_unassigned(item, item.suggested_contact, $button),
				true
			);
		button(__("Assign contact", null, CONTEXT), () => this.pick_contact(item), !item.suggested_contact);
		if (!item.existing_contact) button(__("Create contact", null, CONTEXT), ($button) => this.create_for(item, $button));
		if (item.ignored) button(__("Stop ignoring", null, CONTEXT), ($button) => this.set_ignored(item, false, $button));
		else button(__("Ignore", null, CONTEXT), ($button) => this.set_ignored(item, true, $button));
	}

	pick_contact(item) {
		const dialog = new frappe.ui.Dialog({
			title: __("Assign contact", null, CONTEXT),
			fields: [
				{
					fieldname: "contact",
					fieldtype: "Link",
					options: "Contact",
					label: __("Contact"),
					reqd: 1,
					default: item.existing_contact || item.suggested_contact || undefined,
				},
			],
			primary_action_label: __("Assign", null, CONTEXT),
			primary_action: ({ contact }) => {
				dialog.disable_primary_action();
				this.assign_unassigned(item, contact)
					.then(() => dialog.hide())
					.catch(() => dialog.enable_primary_action());
			},
		});
		dialog.show();
	}

	busy($button, promise) {
		$button?.prop("disabled", true);
		return promise.catch((e) => {
			$button?.prop("disabled", false);
			throw e;
		});
	}

	assign_unassigned(item, contact, $button) {
		const call = frappe.call({
			method: "chatsync.api.assign_chat",
			args: { beeper_chat_id: item.beeper_chat_id, contact },
		});
		return this.busy($button, call).then((r) => this.assigned(item, contact, r.message));
	}

	create_for(item, $button) {
		const call = frappe.call({
			method: "chatsync.api.create_contact_for_chat",
			args: { beeper_chat_id: item.beeper_chat_id },
		});
		return this.busy($button, call).then((r) => this.assigned(item, r.message));
	}

	assigned(item, contact, chat) {
		this.drop_unassigned(item);
		frappe.show_alert({ message: __("Contact assigned", null, CONTEXT), indicator: "green" });
		this.set_segment("conversations");
		frappe.set_route("chat-view", chat ? { contact, select: chat } : { contact });
	}

	drop_unassigned(item) {
		this.unassigned.items = this.unassigned.items.filter((i) => i.beeper_chat_id !== item.beeper_chat_id);
		this.render_unassigned();
	}

	set_ignored(item, ignored, $button) {
		const method = ignored ? "chatsync.api.ignore_chat" : "chatsync.api.unignore_chat";
		const call = frappe.call({ method, type: "POST", args: { beeper_chat_id: item.beeper_chat_id } });
		return this.busy($button, call).then(() => {
			item.ignored = ignored;
			if (ignored && !this.$show_ignored.prop("checked")) {
				this.drop_unassigned(item);
				return this.close();
			}
			this.render_unassigned();
			this.render_preview_actions();
		});
	}

	make_conversation_pane() {
		this.$pane = $(`<section class="chat-conversation-pane"></section>`).appendTo(this.$body);
		this.$empty = $(`<div class="chat-empty"></div>`)
			.append(frappe.utils.icon("message-circle", "lg"))
			.append($(`<div></div>`).text(__("Select a conversation", null, CONTEXT)))
			.appendTo(this.$pane);
		this.$conversation = $(`<div class="chat-conversation"></div>`).appendTo(this.$pane).hide();
		const $header = $(`<header class="chat-header"></header>`).appendTo(this.$conversation);
		$(`<button type="button" class="btn btn-default btn-sm chat-back"></button>`)
			.attr({ title: __("Back", null, CONTEXT), "aria-label": __("Back", null, CONTEXT) })
			.append(frappe.utils.icon("arrow-left", "sm"))
			.on("click", () => (this.mode === "preview" ? this.close() : frappe.set_route("chat-view")))
			.appendTo($header);
		this.$avatar = $(`<div></div>`).appendTo($header);
		const $titles = $(`<div class="chat-titles"></div>`).appendTo($header);
		this.$title = $(`<a class="chat-title"></a>`).appendTo($titles);
		this.$subtitle = $(`<div class="chat-subtitle"></div>`).appendTo($titles);
		this.$actions = $(`<div class="chat-preview-actions"></div>`).appendTo(this.$conversation).hide();
		this.$scroll = $(`<div class="chat-scroll"></div>`).appendTo(this.$conversation);
		this.$more = $(`<div class="chat-more"></div>`).appendTo(this.$scroll);
		this.$list = $(`<div class="chat-messages"></div>`).appendTo(this.$scroll);
		this.$scroll.on("scroll", () => {
			const scroll = this.$scroll[0];
			this.at_bottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
			if (scroll.scrollTop < 40) this.load_older();
		});
		this.make_composer();
	}

	make_composer() {
		const $composer = $(`<footer class="chat-composer"></footer>`).appendTo(this.$conversation);
		this.$network = $(`<select class="form-control chat-network"></select>`)
			.attr({ "aria-label": __("Send via", null, CONTEXT), title: __("Send via", null, CONTEXT) })
			.on("change", () => this.select_chat(this.$network.val()))
			.appendTo($composer);
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

	show({ chat, contact, day, select }) {
		this.load_conversations();
		if (contact || chat) this.set_segment("conversations");
		if (contact) return this.open_contact(contact, { day, select });
		if (chat) return this.open_chat(chat, day);
		this.close();
	}

	close() {
		this.ticket++;
		this.key = null;
		this.mode = null;
		this.preview = null;
		this.$conversation.removeClass("chat-previewing");
		this.$actions.empty().hide();
		this.clear_pending();
		this.page.clear_menu();
		this.page.set_title(__("Conversations", null, CONTEXT));
		this.$body.removeClass("chat-open");
		this.$conversation.hide();
		this.$empty.show();
		this.mark_active();
	}

	load_conversations() {
		frappe.call({ method: "chatsync.api.get_conversations", type: "GET" }).then((r) => {
			this.conversations = r.message || [];
			this.render_conversations();
		});
	}

	render_conversations() {
		const query = (this.$search.val() || "").trim().toLowerCase();
		const rows = this.conversations.filter(
			(row) => !query || `${row.title} ${row.networks.join(" ")} ${row.preview}`.toLowerCase().includes(query)
		);
		this.$conversations.empty();
		if (!rows.length) {
			$(`<div class="chat-list-empty"></div>`)
				.text(query ? __("No matches", null, CONTEXT) : __("No conversations yet", null, CONTEXT))
				.appendTo(this.$conversations);
			return;
		}
		rows.forEach((row) => this.conversation_row(row).appendTo(this.$conversations));
		this.mark_active();
	}

	conversation_row(row) {
		const href = row.contact
			? `/desk/chat-view?contact=${encodeURIComponent(row.contact)}`
			: `/desk/chat-view/${encodeURIComponent(row.chat)}`;
		const $row = $(`<a class="chat-row-item" role="listitem"></a>`).attr({ "data-key": row.key, href });
		$row.append(avatar(row.title));
		const $text = $(`<div class="chat-row-text"></div>`).appendTo($row);
		const $top = $(`<div class="chat-row-top"></div>`).appendTo($text);
		$(`<span class="chat-row-title"></span>`).text(row.title).appendTo($top);
		$(`<span class="chat-row-time"></span>`).text(this.short_time(row.last_message_at)).appendTo($top);
		const $bottom = $(`<div class="chat-row-bottom"></div>`).appendTo($text);
		const prefix = row.direction === "Out" ? `${__("You", null, CONTEXT)}: ` : "";
		$(`<span class="chat-row-preview"></span>`).text(prefix + (row.preview || "")).appendTo($bottom);
		const $networks = $(`<span class="chat-row-networks"></span>`).appendTo($bottom);
		row.networks.forEach((label) => network_chip(label).appendTo($networks));
		$row.on("click", (e) => {
			if (e.metaKey || e.ctrlKey || e.shiftKey) return;
			e.preventDefault();
			if (row.contact) frappe.set_route("chat-view", { contact: row.contact });
			else frappe.set_route("chat-view", row.chat);
		});
		return $row;
	}

	mark_active() {
		this.$body.find(".chat-row-item").each((_, el) => {
			$(el).toggleClass("active", el.dataset.key === this.key);
		});
	}

	short_time(value) {
		if (!value) return "";
		value = String(value);
		if (value.slice(0, 10) === frappe.datetime.get_today()) return value.slice(11, 16);
		return frappe.datetime.str_to_user(value.slice(0, 10));
	}

	open_chat(chat, day) {
		const ticket = ++this.ticket;
		frappe.call({ method: "chatsync.api.get_messages", args: { chat } }).then((r) => {
			if (ticket !== this.ticket) return;
			const data = r.message;
			if (data.chat.contact) return this.open_contact(data.chat.contact, { day, select: chat });
			this.mode = "chat";
			this.key = chat;
			this.contact = null;
			this.chats = [{ name: chat, network: data.chat.network, network_label: data.chat.network_label, title: data.chat.title }];
			this.chat = chat;
			this.title = data.chat.title || chat;
			this.start(data, day);
		});
	}

	open_contact(contact, { day, select } = {}) {
		const ticket = ++this.ticket;
		frappe.call({ method: "chatsync.api.get_contact_messages", type: "GET", args: { contact } }).then((r) => {
			if (ticket !== this.ticket) return;
			const data = r.message;
			this.mode = "contact";
			this.key = contact;
			this.contact = contact;
			this.chats = data.chats;
			this.chat = data.chats.some((c) => c.name === select) ? select : data.default_chat;
			this.title = data.contact_name || contact;
			this.start(data, day);
		});
	}

	async start(data, day) {
		this.data = data;
		this.messages = data.messages;
		this.has_more = data.has_more;
		this.loading = null;
		this.clear_pending();
		this.$body.addClass("chat-open");
		this.$empty.hide();
		this.$conversation.removeClass("chat-previewing").show();
		this.$actions.empty().hide();
		this.render_header();
		this.render_networks();
		this.render_actions();
		this.render();
		this.mark_active();
		this.$input.val("");
		this.update_composer();
		if (day) await this.reveal(day);
		else this.scroll_to_bottom();
	}

	render_header() {
		this.$avatar.empty().append(avatar(this.title));
		this.$title.text(this.title);
		if (this.contact) this.$title.attr("href", frappe.utils.get_form_link("Contact", this.contact));
		else this.$title.removeAttr("href");
		const $subtitle = this.$subtitle.empty();
		[...new Set(this.chats.map((c) => c.network_label))].forEach((label) => network_chip(label).appendTo($subtitle));
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

	network_html() {
		return frappe.utils.escape_html(this.selected().network_label);
	}

	select_chat(chat) {
		this.chat = chat;
		this.clear_pending();
		this.render_networks();
		this.render_actions();
	}

	render_actions() {
		this.page.clear_menu();
		if (!this.chat) return;
		const label = this.network_html();
		this.page.add_menu_item(__("Move {0} chat to another contact", [label], CONTEXT), () => this.reassign());
		this.page.add_menu_item(__("Unassign {0} chat", [label], CONTEXT), () => this.unassign());
		if (this.data.archive_available) {
			this.page.add_menu_item(__("Archive {0} chat", [label], CONTEXT), () => this.archive());
		}
	}

	reassign() {
		const chat = this.chat;
		const dialog = new frappe.ui.Dialog({
			title: __("Move {0} chat to another contact", [this.network_html()], CONTEXT),
			fields: [{ fieldname: "contact", fieldtype: "Link", options: "Contact", label: __("Contact"), reqd: 1 }],
			primary_action_label: __("Reassign", null, CONTEXT),
			primary_action: ({ contact }) => {
				frappe.call({ method: "chatsync.api.reassign_chat", args: { chat, contact } }).then((r) => {
					dialog.hide();
					this.forget_contacts(contact);
					frappe.set_route("chat-view", { contact, select: r.message });
				});
			},
		});
		dialog.show();
	}

	archive() {
		const chat = this.chat;
		const dialog = new frappe.ui.Dialog({
			title: __("Archive {0} chat", [this.network_html()], CONTEXT),
			fields: [
				{ fieldname: "date_from", fieldtype: "Date", label: __("From", null, CONTEXT) },
				{ fieldname: "date_to", fieldtype: "Date", label: __("To", null, CONTEXT) },
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${__("Leave empty to archive the whole chat", null, CONTEXT)}</p>`,
				},
			],
			primary_action_label: __("Archive", null, CONTEXT),
			primary_action: ({ date_from, date_to }) => {
				frappe
					.call({ method: "chatsync.archive.archive_chat", type: "POST", args: { chat, date_from, date_to } })
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
		const chat = this.chat;
		const label = this.network_html();
		frappe.confirm(__("Unassign the {0} chat from the contact? Its messages will be deleted.", [label], CONTEXT), () =>
			frappe.call({ method: "chatsync.api.unassign_chat", args: { chat } }).then(() => {
				this.forget_contacts();
				if (this.contact && this.chats.length > 1) return this.show({ contact: this.contact });
				if (this.contact) return frappe.set_route("Form", "Contact", this.contact);
				frappe.set_route("chat-view");
			})
		);
	}

	forget_contacts(...extra) {
		[this.contact, ...extra].filter(Boolean).forEach((contact) => frappe.model.remove_from_locals("Contact", contact));
	}

	day_of(message) {
		return message.sent_at.slice(0, 10);
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
		const $meta = $(`<div class="chat-meta"></div>`).appendTo($bubble);
		if (this.chats.length > 1 && message.network_label) network_chip(message.network_label).appendTo($meta);
		if (!out && this.mode === "contact" && message.sender_name && message.sender_name !== this.title)
			$(`<span class="chat-sender"></span>`).text(message.sender_name).appendTo($meta);
		$(`<span class="chat-time"></span>`).text(message.sent_at.slice(11, 16)).appendTo($meta);
		return $(`<div class="chat-row"></div>`).toggleClass("out", out).attr("data-name", message.name).append($bubble);
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
		if (!this.messages.length)
			$(`<div class="chat-list-empty"></div>`).text(__("No messages yet", null, CONTEXT)).appendTo(this.$list);
		this.$pending?.appendTo(this.$list);
		this.$more.text(this.has_more ? __("Loading older messages...") : "");
	}

	load_older() {
		if (!this.has_more || !this.messages.length) return Promise.resolve(false);
		this.loading ||= this.fetch_older().finally(() => (this.loading = null));
		return this.loading;
	}

	async fetch_older() {
		const ticket = this.ticket;
		const before = this.messages[0].name;
		const r =
			this.mode === "contact"
				? await frappe.call({
						method: "chatsync.api.get_contact_messages",
						type: "GET",
						args: { contact: this.contact, before },
					})
				: await frappe.call({ method: "chatsync.api.get_messages", args: { chat: this.chat, before } });
		if (ticket !== this.ticket) return false;
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
		this.at_bottom = false;
		this.$scroll.scrollTop(target.offsetTop - this.$scroll[0].offsetTop);
	}

	scroll_to_bottom() {
		this.at_bottom = true;
		this.$scroll.scrollTop(this.$scroll[0].scrollHeight);
	}

	merge(incoming, chat) {
		const info = this.chats.find((c) => c.name === chat) || {};
		const known = new Set(this.messages.map((m) => m.name));
		const added = incoming
			.filter((m) => !known.has(m.name))
			.map((m) => ({ ...m, chat, network: info.network, network_label: info.network_label }));
		this.messages = this.messages
			.concat(added)
			.sort((a, b) => a.sent_at.localeCompare(b.sent_at) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
		this.render();
		return added;
	}

	show_pending(text) {
		this.clear_pending();
		const $meta = $(`<div class="chat-meta"></div>`);
		if (this.chats.length > 1) network_chip(this.selected().network_label).appendTo($meta);
		$(`<span class="chat-time"></span>`).text(__("Sending...", null, CONTEXT)).appendTo($meta);
		this.$pending = $(`<div class="chat-row out chat-pending"></div>`).append(
			$(`<div class="chat-bubble"></div>`).append($(`<div class="chat-text"></div>`).text(text)).append($meta)
		);
		this.$pending.appendTo(this.$list);
		this.scroll_to_bottom();
	}

	clear_pending() {
		this.$pending?.remove();
		this.$pending = null;
		clearTimeout(this.poll_timer);
	}

	poll_sent(chat, text, tries = 4) {
		const ticket = this.ticket;
		this.poll_timer = setTimeout(async () => {
			if (!this.$pending) return;
			let r;
			try {
				r = await frappe.call({ method: "chatsync.api.refresh_messages", type: "POST", args: { chat } });
			} catch (e) {
				return this.give_up(ticket);
			}
			if (ticket !== this.ticket) return;
			const added = this.merge(r.message.messages, chat);
			if (added.some((m) => m.direction === "Out" && m.text === text)) {
				this.clear_pending();
				this.scroll_to_bottom();
			} else if (tries > 1) {
				this.poll_sent(chat, text, tries - 1);
			} else {
				this.give_up(ticket);
			}
		}, 2500);
	}

	give_up(ticket) {
		if (ticket !== this.ticket) return;
		this.clear_pending();
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
		this.sending = true;
		this.$input.prop("disabled", true);
		this.update_composer();
		frappe
			.call({ method: "chatsync.api.send_message", args: { chat, text } })
			.then((r) => {
				if (ticket !== this.ticket) return;
				this.$input.val("");
				const added = this.merge(r.message.messages, chat);
				if (added.some((m) => m.direction === "Out" && m.text === text)) {
					this.scroll_to_bottom();
				} else {
					this.show_pending(text);
					this.poll_sent(chat, text);
				}
				this.load_conversations();
			})
			.always(() => {
				this.sending = false;
				this.$input.prop("disabled", false).focus();
				this.update_composer();
			});
	}
}
