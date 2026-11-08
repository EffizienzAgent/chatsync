from chatsync.formatting import plain, render
from chatsync.tests.helpers import ChatsyncTestCase


class TestRenderHtml(ChatsyncTestCase):
	def test_keeps_allowed_tags(self):
		self.assertEqual(render("<p>Hi Max!</p><p>LG!</p>"), "<p>Hi Max!</p><p>LG!</p>")
		html = "<b>b</b><strong>s</strong><i>i</i><em>e</em><s>s</s><del>d</del><u>u</u><code>c</code>"
		self.assertEqual(render(html), html)
		lists = "<blockquote>q</blockquote><ul><li>a</li></ul><ol><li>b</li></ol><pre>x</pre>a<br>b"
		self.assertEqual(render(lists), lists)

	def test_removes_scripts_and_handlers(self):
		html = render('<p onclick="x()">Hi<script>alert(1)</script><img src=x onerror="alert(1)"></p>')
		self.assertEqual(html, "<p>Hi</p>")

	def test_strips_unknown_tags_keeps_text(self):
		self.assertEqual(render('<div class="x"><span style="color:red">Hi</span></div>'), "Hi")

	def test_links_only_safe_schemes(self):
		self.assertEqual(
			render('<a href="https://example.com" title="t">x</a>'),
			'<a href="https://example.com" target="_blank" rel="noopener noreferrer">x</a>',
		)
		self.assertIn('href="mailto:a@b.de"', render('<a href="mailto:a@b.de">m</a>'))
		for href in ("javascript:alert(1)", "data:text/html,x", "JaVaScRiPt:alert(1)"):
			with self.subTest(href):
				self.assertNotIn("href", render(f'<a href="{href}">x</a>'))

	def test_empty(self):
		self.assertEqual(render(None), "")
		self.assertEqual(render(""), "")


class TestRenderPlainText(ChatsyncTestCase):
	def test_escapes(self):
		self.assertEqual(render("a < b & c > d"), "a &lt; b &amp; c &gt; d")
		self.assertEqual(render('"quoted"'), "&quot;quoted&quot;")

	def test_whatsapp_markup(self):
		self.assertEqual(render("*bold*"), "<strong>bold</strong>")
		self.assertEqual(render("_italic_"), "<em>italic</em>")
		self.assertEqual(render("~strike~"), "<s>strike</s>")
		self.assertEqual(render("say *hi* and _bye_."), "say <strong>hi</strong> and <em>bye</em>.")
		self.assertEqual(render("*_both_*"), "<strong><em>both</em></strong>")

	def test_markup_needs_word_boundaries(self):
		for text in ("snake_case_name", "2*3*4", "a~b~c", "* not bold *", "file_name_v2.txt"):
			with self.subTest(text):
				self.assertEqual(render(text), text)

	def test_markup_does_not_cross_lines(self):
		self.assertEqual(render("*a\nb*"), "*a<br>b*")

	def test_monospace(self):
		self.assertEqual(render("```x = *y*```"), "<pre>x = *y*</pre>")
		self.assertEqual(render("```a\nb```"), "<pre>a\nb</pre>")
		self.assertEqual(render("run `ls -la` now"), "run <code>ls -la</code> now")
		self.assertEqual(render("`a < b`"), "<code>a &lt; b</code>")

	def test_linkify(self):
		self.assertEqual(
			render("see https://example.com/a_b_c?x=1&y=2."),
			'see <a href="https://example.com/a_b_c?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">'
			"https://example.com/a_b_c?x=1&amp;y=2</a>.",
		)
		underscores = render("check http://x.de/_a_b_ ok")
		self.assertNotIn("<em>", underscores)
		self.assertIn('href="http://x.de/_a_b_"', underscores)
		self.assertIn("<strong>", render("*see https://x.de*"))
		self.assertEqual(render("ftp://x.de"), "ftp://x.de")

	def test_url_with_quotes_stays_inside_attribute(self):
		html = render('https://x.de/"onmouseover="alert(1)')
		self.assertNotIn('"onmouseover', html)

	def test_newlines(self):
		self.assertEqual(render("a\nb\r\nc"), "a<br>b<br>c")


class TestPlain(ChatsyncTestCase):
	def test_html_to_text(self):
		self.assertEqual(plain("<p>Hi Max!</p><p>LG &amp; bis bald!</p>"), "Hi Max!\nLG & bis bald!")
		self.assertEqual(plain("a<br>b<br/>c"), "a\nb\nc")
		self.assertEqual(plain("<ul><li>a</li><li>b</li></ul>"), "a\nb")
		self.assertEqual(plain("<p>x<script>alert(1)</script></p>"), "x")

	def test_plain_text_unchanged(self):
		self.assertEqual(plain("*bold* a < b & c\nnext"), "*bold* a < b & c\nnext")

	def test_empty(self):
		self.assertEqual(plain(None), "")
