from urllib.parse import quote

import requests

VOICE_FALLBACK = "audio/ogg"
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class BeeperError(Exception):
	def __init__(self, status: int, message: str):
		super().__init__(f"Beeper API {status}: {message}")
		self.status = status
		self.message = message


class BeeperAuthError(BeeperError):
	pass


class BeeperClient:
	def __init__(self, base_url: str, token: str, timeout: int = 30):
		self.base_url = base_url.rstrip("/")
		self.timeout = timeout
		self.headers = {"Authorization": f"Bearer {token}"}
		self.session = requests.Session()

	def chats(self, cursor: str | None = None) -> tuple[list[dict], str | None]:
		return self._page("/v1/chats", {"direction": "before", **_cursor(cursor)}, "before")

	def chat(self, chat_id: str) -> dict:
		return self._get(f"/v1/chats/{quote(chat_id, safe='')}").json()

	def search_chats(self, query: str, limit: int = 20) -> list[dict]:
		params = {"query": query, "type": "single", "limit": limit}
		return self._get("/v1/chats/search", params).json()["items"]

	def messages(
		self, chat_id: str, cursor: str | None = None, direction: str = "before"
	) -> tuple[list[dict], str | None]:
		params = {"direction": direction, **_cursor(cursor)}
		return self._page(f"/v1/chats/{quote(chat_id, safe='')}/messages", params, direction)

	def send_text(self, chat_id: str, text: str) -> dict:
		path = f"/v1/chats/{quote(chat_id, safe='')}/messages"
		return self._request("POST", path, json={"text": text}).json()

	def asset(self, url: str) -> tuple[bytes, str]:
		response = self._get("/v1/assets/serve", {"url": url})
		content_type = response.headers.get("Content-Type")
		if not content_type:
			content_type = VOICE_FALLBACK if _is_voice(url) else DEFAULT_CONTENT_TYPE
		return response.content, content_type

	def setup_state(self) -> str:
		return self._get("/v1/app/setup").json()["state"]

	def _page(self, path: str, params: dict, direction: str) -> tuple[list[dict], str | None]:
		data = self._get(path, params).json()
		cursor = data.get("oldestCursor" if direction == "before" else "newestCursor")
		return data["items"], cursor if data.get("hasMore") else None

	def _get(self, path: str, params: dict | None = None):
		return self._request("GET", path, params=params)

	def _request(self, method: str, path: str, **kwargs):
		try:
			response = self.session.request(
				method, self.base_url + path, headers=self.headers, timeout=self.timeout, **kwargs
			)
		except requests.RequestException as e:
			raise BeeperError(0, type(e).__name__) from None
		if response.ok:
			return response
		error = BeeperAuthError if response.status_code == 401 else BeeperError
		raise error(response.status_code, _message(response))


def _cursor(cursor: str | None) -> dict:
	return {"cursor": cursor} if cursor else {}


def _is_voice(url: str) -> bool:
	name = url.lower().split("?")[0]
	return name.endswith(".ogg") or "voice" in name


def _message(response) -> str:
	try:
		return str(response.json().get("message", ""))
	except ValueError, AttributeError:
		return response.text[:200]
