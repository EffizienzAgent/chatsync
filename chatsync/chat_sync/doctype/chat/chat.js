frappe.ui.form.on("Chat", {
	refresh(frm) {
		if (frm.is_new()) return;
		const day = new URLSearchParams(window.location.search).get("day");
		frappe.route_options = day ? { day } : null;
		frappe.set_route("chat-view", frm.doc.name);
	},
});
