from unittest.mock import patch

import requests
from frappe.tests import UnitTestCase

from chatsync.beeper import BeeperAuthError, BeeperClient, BeeperError

BASE = "https://beeper.example.test"
TOKEN = "token-1a2b3c"
CHAT_ID = "!abc:beeper.example"


class FakeResponse:
	def __init__(self, payload=None, status_code=200, content=b"", headers=None):
		self.payload = payload
		self.status_code = status_code
		self.content = content
		self.headers = headers or {}
		self.text = str(payload)

	@property
	def ok(self):
		return self.status_code < 400

	def json(self):
		return self.payload


class TestBeeperClient(UnitTestCase):
	def setUp(self):
		self.client = BeeperClient(BASE, TOKEN)

	def serve(self, *responses):
		queue = list(responses)
		self.calls = []

		def request(session, method, url, **kwargs):
			self.calls.append((method, url, kwargs))
			return queue.pop(0)

		return patch.object(requests.Session, "request", autospec=True, side_effect=request)

	def test_paths_and_auth_header(self):
		page = {"items": [{"id": 1}], "hasMore": False, "oldestCursor": "o", "newestCursor": "n"}
		with self.serve(FakeResponse(page), FakeResponse(page), FakeResponse({"items": []})):
			self.client.chats("c1")
			self.client.messages(CHAT_ID, direction="after")
			self.client.search_chats("anna", limit=5)
		(_, url, kw), (_, murl, mkw), (_, surl, skw) = self.calls
		self.assertEqual(url, f"{BASE}/v1/chats")
		self.assertEqual(kw["params"], {"direction": "before", "cursor": "c1"})
		self.assertEqual(murl, f"{BASE}/v1/chats/%21abc%3Abeeper.example/messages")
		self.assertEqual(mkw["params"], {"direction": "after"})
		self.assertEqual(surl, f"{BASE}/v1/chats/search")
		self.assertEqual(skw["params"], {"query": "anna", "type": "single", "limit": 5})
		self.assertEqual(kw["headers"]["Authorization"], f"Bearer {TOKEN}")

	def test_send_text(self):
		with self.serve(FakeResponse({"chatID": CHAT_ID, "pendingMessageID": "7"})):
			result = self.client.send_text(CHAT_ID, "Hallo")
		method, url, kw = self.calls[0]
		self.assertEqual(method, "POST")
		self.assertTrue(url.endswith("/v1/chats/%21abc%3Abeeper.example/messages"))
		self.assertEqual(kw["json"], {"text": "Hallo"})
		self.assertEqual(result["pendingMessageID"], "7")

	def test_pagination_cursor(self):
		first = {"items": [1], "hasMore": True, "oldestCursor": "old", "newestCursor": "new"}
		last = {"items": [2], "hasMore": False, "oldestCursor": "older", "newestCursor": "new2"}
		with self.serve(FakeResponse(first), FakeResponse(last), FakeResponse(first)):
			items, cursor = self.client.chats()
			self.assertEqual((items, cursor), ([1], "old"))
			items, cursor = self.client.chats(cursor)
			self.assertEqual((items, cursor), ([2], None))
			self.assertEqual(self.client.messages(CHAT_ID, direction="after")[1], "new")

	def test_401_raises_auth_error(self):
		with self.serve(FakeResponse({"message": "bad token"}, status_code=401)):
			with self.assertRaises(BeeperAuthError) as ctx:
				self.client.chats()
		self.assertEqual(ctx.exception.status, 401)

	def test_error_carries_status(self):
		with self.serve(FakeResponse({"message": "boom"}, status_code=500)):
			with self.assertRaises(BeeperError) as ctx:
				self.client.setup_state()
		self.assertNotIsInstance(ctx.exception, BeeperAuthError)
		self.assertEqual(ctx.exception.status, 500)

	def test_setup_state(self):
		with self.serve(FakeResponse({"state": "ready"})):
			self.assertEqual(self.client.setup_state(), "ready")

	def test_asset_content_type_fallback(self):
		with self.serve(
			FakeResponse(content=b"a"),
			FakeResponse(content=b"b", headers={"Content-Type": "audio/mpeg"}),
			FakeResponse(content=b"c"),
		):
			self.assertEqual(self.client.asset("mxc://x/voice.ogg"), (b"a", "audio/ogg"))
			self.assertEqual(self.client.asset("mxc://x/a"), (b"b", "audio/mpeg"))
			self.assertEqual(self.client.asset("mxc://x/blob"), (b"c", "application/octet-stream"))
		self.assertEqual(self.calls[0][2]["params"], {"url": "mxc://x/voice.ogg"})
