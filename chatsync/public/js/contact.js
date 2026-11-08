frappe.ui.form.on("Contact", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Assign chat"), () => assign_chat_dialog(frm), __("Actions"));
		add_chat_action(frm);
	},
});

function add_chat_action(frm) {
	const actions = frm.timeline?.timeline_actions_wrapper;
	if (!frappe.boot.chatsync_can_chat || !actions || actions.find(".chatsync-chat").length) return;
	frm.timeline.add_action_button(__("Chat", null, "Contact action"), () => open_chat(frm), "message-circle").addClass("chatsync-chat");
}

async function open_chat(frm) {
	const contact = frm.doc.name;
	const count = await frappe.db.count("Chat", { filters: { contact, user: frappe.session.user } });
	if (count) return frappe.set_route("chat-view", { contact });
	const r = await frappe.call({
		method: "chatsync.api.chat_start_options",
		type: "GET",
		args: { contact },
		freeze: true,
	});
	start_chat_dialog(contact, r.message);
}

function start_chat_dialog(contact, { accounts, phones }) {
	if (!phones.length) return frappe.msgprint(__("This contact has no phone number. Add one, or assign an existing chat."));
	if (!accounts.length) return frappe.msgprint(__("No WhatsApp or Signal account is connected in Beeper."));
	const dialog = new frappe.ui.Dialog({
		title: __("Start chat"),
		fields: [
			{
				fieldname: "account_id",
				fieldtype: "Select",
				label: __("Account"),
				reqd: 1,
				options: accounts.map((a) => ({
					value: a.account_id,
					label: frappe.utils.escape_html([a.network_label, a.name].filter(Boolean).join(" · ")),
				})),
				default: accounts[0].account_id,
			},
			{ fieldname: "phone", fieldtype: "Select", label: __("Phone"), reqd: 1, options: phones, default: phones[0] },
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__("Nothing is sent yet. Write the first message in the chat view.")}</p>`,
			},
		],
		primary_action_label: __("Start chat"),
		primary_action: ({ account_id, phone }) => {
			dialog.disable_primary_action();
			frappe
				.call({ method: "chatsync.api.start_chat", type: "POST", args: { contact, account_id, phone }, freeze: true })
				.then((r) => {
					dialog.hide();
					frappe.model.remove_from_locals("Contact", contact);
					frappe.set_route("chat-view", { contact, select: r.message });
				})
				.catch(() => dialog.enable_primary_action());
		},
	});
	dialog.show();
}

function assign_chat_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Assign chat"),
		fields: [
			{ fieldname: "query", fieldtype: "Data", label: __("Search"), reqd: 1 },
			{ fieldname: "results", fieldtype: "HTML" },
		],
	});
	const results = () => dialog.get_field("results").$wrapper;
	const search = frappe.utils.debounce((query) => {
		if (!query || query.length < 2) return results().empty();
		frappe
			.call({ method: "chatsync.api.search_chats", args: { query } })
			.then((r) => render_results(r.message || []));
	}, 400);

	const render_results = (chats) => {
		const wrapper = results().empty();
		if (!chats.length) return wrapper.append(`<p class="text-muted">${__("No chats found")}</p>`);
		chats.forEach((chat) => {
			const detail = [chat.network, chat.phone || chat.username].filter(Boolean).join(" · ");
			$(`<div class="list-group-item list-group-item-action" style="cursor: pointer"></div>`)
				.append($("<strong>").text(chat.title || chat.beeper_chat_id))
				.append($('<div class="text-muted small">').text(detail))
				.appendTo(wrapper)
				.on("click", () => assign(chat));
		});
	};

	const assign = (chat) => {
		frappe
			.call({
				method: "chatsync.api.assign_chat",
				args: { beeper_chat_id: chat.beeper_chat_id, contact: frm.doc.name },
				freeze: true,
			})
			.then(() => {
				dialog.hide();
				frm.reload_doc();
			});
	};

	dialog.get_field("query").$input.on("input", (e) => search(e.target.value.trim()));
	dialog.show();
}
