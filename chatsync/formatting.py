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
HTML_ELEMENT = re.compile(rf"<({'|'.join(sorted(TAGS))})(\s[^<>]*)?>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
HTML_START = re.compile(r"\s*<p[\s>]", re.IGNORECASE)
LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
ABSOLUTE_LINK = re.compile(r"(https?://|mailto:)", re.IGNORECASE)
BLOCK_END = re.compile(r"<br\s*/?>|</(p|li|div|blockquote|pre|ul|ol|h[1-6])>", re.IGNORECASE)

PLACEHOLDER = re.compile("\x00(\\d+)\x00")
MONO_BLOCK = re.compile(r"```(.+?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
URL = re.compile(r"https?://[^\s<>\"'`\x00]+")
URL_TRAILING = ".,;:!?*~)"
MARKUP = [
	(re.compile(rf"(?<![\w{m}]){m}(?=\S)([^\n]*?\S){m}(?![\w{m}])"), tag)
	for m, tag in (("\\*", "strong"), ("_", "em"), ("~", "s"))
]


def is_html(text: str) -> bool:
	"""Only real markup counts: an allowlisted element with its closing tag, a leading <p>, or <br>."""
	return bool(HTML_START.match(text) or LINE_BREAK.search(text) or HTML_ELEMENT.search(text))


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
		attribute_filter=absolute_links_only,
	)


def absolute_links_only(_tag: str, attribute: str, value: str) -> str | None:
	if attribute == "href" and not ABSOLUTE_LINK.match(value.strip()):
		return None
	return value


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
	"""Text without tags for previews, transcripts and tools; plain text passes unchanged.

	The result is raw text (entities decoded, "<" kept): escape it before putting it into HTML.
	"""
	if not text:
		return ""
	if not is_html(text):
		return text
	text = BLOCK_END.sub(lambda m: m.group(0) + "\n", text)
	text = html.unescape(nh3.clean(text, tags=set()))
	text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
	return re.sub(r"\n{3,}", "\n\n", text).strip()


def preview(text: str | None) -> str:
	"""One line of text without markup (HTML or WhatsApp style), for lists."""
	if not text:
		return ""
	if is_html(text):
		return " ".join(plain(text).split())
	return " ".join(html.unescape(nh3.clean(render_markup(text).replace("<br>", " "), tags=set())).split())
