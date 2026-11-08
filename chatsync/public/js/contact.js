frappe.ui.form.on("Contact", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Assign chat"), () => assign_chat_dialog(frm), __("Actions"));
	},
});

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
