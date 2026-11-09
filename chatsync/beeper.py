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


class AssetTooLarge(Exception):
	def __init__(self, limit: int):
		super().__init__(f"Asset larger than {limit} bytes")
		self.limit = limit


class BeeperClient:
	def __init__(self, base_url: str, token: str, timeout: int = 30):
		self.base_url = base_url.rstrip("/")
		self.timeout = timeout
		self.headers = {"Authorization": f"Bearer {token}"}
		self.session = requests.Session()

	def chats(self, cursor: str | None = None, limit: int | None = None) -> tuple[list[dict], str | None]:
		params = {"direction": "before", **_cursor(cursor), **({"limit": limit} if limit else {})}
		return self._page("/v1/chats", params, "before")

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

	def accounts(self) -> list[dict]:
		return self._get("/v1/accounts").json()

	def start_chat(self, account_id: str, user: dict) -> dict:
		"""Opens (or reuses) a direct chat without sending anything; returns the chat."""
		payload = {"accountID": account_id, "user": user, "allowInvite": False}
		chat = self._request("POST", "/v1/chats/start", json=payload).json()
		chat.setdefault("id", chat.get("chatID"))
		return chat

	def asset(self, url: str, limit: int | None = None) -> tuple[bytes, str]:
		response = self._asset_response(url)
		content_type = response.headers.get("Content-Type")
		if not content_type:
			content_type = VOICE_FALLBACK if _is_voice(url) else DEFAULT_CONTENT_TYPE
		return _read(response, limit), content_type

	def asset_part(
		self, url: str, range_header: str, limit: int | None = None
	) -> tuple[bytes, str, int, str | None]:
		"""The Beeper Server answers Range requests with 206; a 200 means it sent the whole file."""
		response = self._asset_response(url, {"Range": range_header})
		content_type = response.headers.get("Content-Type") or DEFAULT_CONTENT_TYPE
		data = _read(response, limit)
		return data, content_type, response.status_code, response.headers.get("Content-Range")

	def _asset_response(self, url: str, headers: dict | None = None):
		return self._request(
			"GET", "/v1/assets/serve", params={"url": url}, headers=headers or {}, stream=True
		)

	def setup_state(self) -> str:
		return self._get("/v1/app/setup").json()["state"]

	def _page(self, path: str, params: dict, direction: str) -> tuple[list[dict], str | None]:
		data = self._get(path, params).json()
		cursor = data.get("oldestCursor" if direction == "before" else "newestCursor")
		return data["items"], cursor if data.get("hasMore") else None

	def _get(self, path: str, params: dict | None = None):
		return self._request("GET", path, params=params)

	def _request(self, method: str, path: str, headers: dict | None = None, **kwargs):
		try:
			response = self.session.request(
				method,
				self.base_url + path,
				headers={**self.headers, **(headers or {})},
				timeout=self.timeout,
				**kwargs,
			)
		except requests.RequestException as e:
			raise BeeperError(0, type(e).__name__) from None
		if response.ok:
			return response
		error = BeeperAuthError if response.status_code == 401 else BeeperError
		raise error(response.status_code, _message(response))


def _read(response, limit: int | None) -> bytes:
	length = response.headers.get("Content-Length")
	if limit and length and length.isdigit() and int(length) > limit:
		response.close()
		raise AssetTooLarge(limit)
	chunks, size = [], 0
	for chunk in response.iter_content(64 * 1024):
		size += len(chunk)
		if limit and size > limit:
			response.close()
			raise AssetTooLarge(limit)
		chunks.append(chunk)
	return b"".join(chunks)


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
