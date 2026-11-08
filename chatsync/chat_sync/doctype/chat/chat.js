frappe.ui.form.on("Chat", {
	refresh(frm) {
		if (frm.is_new()) return;
		const day = new URLSearchParams(window.location.search).get("day");
		const query = day ? `?day=${encodeURIComponent(day)}` : "";
		history.replaceState(null, "", `/desk/chat-view/${encodeURIComponent(frm.doc.name)}${query}`);
		frappe.router.route();
	},
});
