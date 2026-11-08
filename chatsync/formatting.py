"""Beeper message text is either HTML (e.g. from Beeper Desktop) or plain text with WhatsApp markup."""

import html
import re

import nh3

TAGS = {
	"p",
	"br",
	"b",
	"strong",
	"i",
	"em",
	"s",
	"del",
	"strike",
	"u",
	"code",
	"pre",
	"blockquote",
	"ul",
	"ol",
	"li",
	"a",
}
URL_SCHEMES = {"http", "https", "mailto"}
LINK_REL = "noopener noreferrer"
HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(\s[^<>]*)?/?>")
BLOCK_END = re.compile(r"<br\s*/?>|</(p|li|div|blockquote|pre|ul|ol|h[1-6])>", re.IGNORECASE)

PLACEHOLDER = re.compile("\x00(\\d+)\x00")
MONO_BLOCK = re.compile(r"```(.+?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
URL = re.compile(r"https?://[^\s<>\"'`]+")
URL_TRAILING = ".,;:!?*~)"
MARKUP = [
	(re.compile(rf"(?<![\w{m}]){m}(?=\S)([^\n]*?\S){m}(?![\w{m}])"), tag)
	for m, tag in (("\\*", "strong"), ("_", "em"), ("~", "s"))
]


def is_html(text: str) -> bool:
	return bool(HTML_TAG.search(text))


def render(text: str | None) -> str:
	"""Safe HTML for the chat view."""
	if not text:
		return ""
	if is_html(text):
		return sanitize(text)
	return render_markup(text)


def sanitize(text: str) -> str:
	return nh3.clean(
		text,
		tags=TAGS,
		attributes={"a": {"href"}, "*": set()},
		url_schemes=URL_SCHEMES,
		link_rel=LINK_REL,
		set_tag_attribute_values={"a": {"target": "_blank"}},
	)


def link(url: str) -> str:
	escaped = html.escape(url)
	return f'<a href="{escaped}" target="_blank" rel="{LINK_REL}">{escaped}</a>'


def split_url(match: re.Match) -> tuple[str, str]:
	url = match.group(0)
	end = len(url)
	while end and url[end - 1] in URL_TRAILING and not (url[end - 1] == ")" and "(" in url[:end]):
		end -= 1
	return url[:end], url[end:]


def render_markup(text: str) -> str:
	text = text.replace("\x00", "").replace("\r\n", "\n")
	pieces: list[str] = []

	def keep(fragment: str) -> str:
		pieces.append(fragment)
		return f"\x00{len(pieces) - 1}\x00"

	def keep_url(match: re.Match) -> str:
		url, rest = split_url(match)
		return keep(link(url)) + rest

	text = MONO_BLOCK.sub(lambda m: keep(f"<pre>{html.escape(m.group(1))}</pre>"), text)
	text = INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
	text = URL.sub(keep_url, text)
	text = html.escape(text)
	for pattern, tag in MARKUP:
		text = pattern.sub(rf"<{tag}>\1</{tag}>", text)
	text = text.replace("\n", "<br>")
	return PLACEHOLDER.sub(lambda m: pieces[int(m.group(1))], text)


def plain(text: str | None) -> str:
	"""Text without tags for previews, transcripts and tools; plain text passes unchanged."""
	if not text:
		return ""
	if not is_html(text):
		return text
	text = BLOCK_END.sub(lambda m: m.group(0) + "\n", text)
	text = html.unescape(nh3.clean(text, tags=set()))
	text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
	return re.sub(r"\n{3,}", "\n\n", text).strip()
