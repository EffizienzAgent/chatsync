LABELS = {
	"whatsapp": "WhatsApp",
	"signal": "Signal",
	"linkedin": "LinkedIn",
	"telegram": "Telegram",
	"instagram": "Instagram",
	"facebook": "Messenger",
	"messenger": "Messenger",
	"imessage": "iMessage",
	"gmessages": "SMS",
	"sms": "SMS",
	"discord": "Discord",
	"slack": "Slack",
	"twitter": "X",
	"matrix": "Beeper",
}


def key(network: str | None) -> str:
	"""Beeper account IDs look like `whatsapp`, `instagramgo` or `local-whatsapp_ba_…`."""
	value = (network or "").lower().removeprefix("local-").split("_")[0].split(".")[0]
	if value not in LABELS and value.endswith("go"):
		value = value[:-2]
	return value


def label(network: str | None) -> str:
	return LABELS.get(key(network)) or (network or "").capitalize()
