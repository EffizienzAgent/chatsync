app_name = "chatsync"
app_title = "Chat Sync"
app_publisher = "EffizienzAgent (Tobias Mende)"
app_description = "Chat history from Beeper for the Frappe Framework"
app_email = "oss@effizienzagent.de"
app_license = "mit"

fixtures = [{"dt": "Custom Field", "filters": [["module", "=", "Chat Sync"]]}]

before_install = "chatsync.install.ensure_role"
after_sync = "chatsync.install.after_migrate"
after_migrate = "chatsync.install.after_migrate"

permission_query_conditions = {
	"Chat Account": "chatsync.permissions.chat_account_conditions",
	"Chat": "chatsync.permissions.query_conditions",
	"Chat Message": "chatsync.permissions.chat_message_conditions",
}

has_permission = {
	"Chat Account": "chatsync.permissions.chat_account_permission",
	"Chat": "chatsync.permissions.has_permission",
	"Chat Message": "chatsync.permissions.has_permission",
}

scheduler_events = {"cron": {"*/2 * * * *": ["chatsync.sync.sync_all"]}}

doc_events = {
	"Contact": {
		"before_save": "chatsync.sync.keep_chat_identifiers",
		"on_update": "chatsync.sync.on_contact_update",
	}
}

doctype_js = {"Contact": "public/js/contact.js"}

docarchive_assign = ["chatsync.archive.assign"]
