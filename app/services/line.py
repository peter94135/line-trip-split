"""LINE Messaging API 的簽章、回覆與可安全重試的請求。"""

import base64
import hashlib
import hmac
import logging
import time
from functools import lru_cache
from urllib.parse import quote

import httpx

from app.config import get_settings

API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
RETRY_DELAYS = (1, 3, 10)
log = logging.getLogger(__name__)
Button = tuple[str, str]


class LineError(RuntimeError):
    def __init__(self, message: str, *, retryable=False, reply_unaccepted=False):
        super().__init__(message)
        self.retryable = retryable
        self.reply_unaccepted = reply_unaccepted


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.line_channel_secret and settings.line_channel_access_token)


def verify_signature(body: bytes, signature: str | None) -> bool:
    secret = get_settings().line_channel_secret
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest())
    return hmac.compare_digest(expected, signature.encode())


def _with_retry(call):
    for delay in RETRY_DELAYS:
        try:
            return call()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            log.warning("連不到 LINE，%s 秒後重試", delay)
            time.sleep(delay)
    return call()


def _headers() -> dict[str, str]:
    token = get_settings().line_channel_access_token
    if not token:
        raise LineError("尚未設定 LINE_CHANNEL_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}"}


def _request(method, path, payload=None, *, retry_key=None) -> dict:
    headers = _headers()
    if retry_key:
        headers["X-Line-Retry-Key"] = retry_key
    try:
        response = _with_retry(lambda: httpx.request(
            method, f"{API}{path}", json=payload, headers=headers, timeout=10,
        ))
    except httpx.HTTPError as exc:
        raise LineError("連不到 LINE", retryable=True, reply_unaccepted=isinstance(
            exc, (httpx.ConnectError, httpx.ConnectTimeout),
        )) from exc
    if retry_key and response.status_code == 409 and response.headers.get("x-line-accepted-request-id"):
        return {}
    if response.is_error:
        expired = response.status_code == 400 and "invalid reply token" in response.text.lower()
        raise LineError(f"LINE API HTTP {response.status_code}", retryable=response.status_code >= 500,
                        reply_unaccepted=expired)
    return response.json() if response.content else {}


def group_count(group_id: str | None) -> int | None:
    if not group_id or not get_settings().line_channel_access_token:
        return None
    # 群組人數很少變動，記住 10 分鐘，網頁開帳本時多半不必等 LINE。
    return _cached_count(group_id, int(time.monotonic() // 600))


@lru_cache(maxsize=256)
def _cached_count(group_id: str, window: int) -> int | None:
    try:
        response = httpx.get(f"{API}/group/{quote(group_id, safe='')}/members/count", headers=_headers(), timeout=2)
        response.raise_for_status()
        count = response.json().get("count")
        return count if type(count) is int and count >= 0 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def get_profile(user_id: str, group_id: str | None = None) -> dict:
    user = quote(user_id, safe="")
    path = f"/group/{quote(group_id, safe='')}/member/{user}" if group_id else f"/profile/{user}"
    try:
        return _request("GET", path)
    except LineError:
        return {}


def get_content(message_id: str, *, max_bytes: int) -> tuple[bytes, str]:
    def download() -> tuple[bytes, str]:
        with httpx.stream("GET", f"{DATA_API}/message/{quote(message_id, safe='')}/content",
                          headers=_headers(), timeout=30) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise LineError("照片太大，請傳小於 10 MB 的收據照片。")
                data.extend(chunk)
            mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
            return bytes(data), mime.strip().lower()

    try:
        return _with_retry(download)
    except httpx.HTTPError as exc:
        raise LineError("這張照片目前無法讀取，請重傳收據後再試。") from exc


def _limit(text: str, units: int) -> str:
    return text.encode("utf-16-le")[: units * 2].decode("utf-16-le", errors="ignore")


def _postback(label: str, data: str, fill: list[str]) -> dict:
    action = {"type": "postback", "label": _limit(label, 20), "data": data}
    if fill:
        # 按下時打開鍵盤並預填文字，送出的文字訊息才是真正的指令。
        action.update(inputOption="openKeyboard", fillInText=_limit(fill[0], 300))
    return action


def _quick_reply(buttons: list[Button | tuple[str, str, str]]) -> dict:
    return {"items": [{"type": "action", "action": _postback(label, data, fill)} for label, data, *fill in buttons[:13]]}


def text_message(text: str, buttons: list[Button | tuple[str, str, str]] | None = None) -> dict:
    """按鈕為 Quick Reply，下一則訊息後就會消失。需要保留的按鈕請用 card_message()。"""
    message = {"type": "text", "text": _limit(text, 5000)}
    if buttons:
        message["quickReply"] = _quick_reply(buttons)
    return message


def postback_button(label: str, data: str, *fill: str) -> dict:
    return {"type": "button", "style": "secondary", "height": "sm", "action": _postback(label, data, list(fill))}


def card_message(text: str, buttons: list[Button | tuple[str, str, str]], *, columns: int = 2,
                 quick: list[Button] | None = None) -> dict:
    """按鈕固定在訊息內的 Flex 卡片。Quick Reply 會在下一則訊息後消失，這裡的按鈕會保留。

    按鈕為 (標籤, postback data)，或 (標籤, data, 預填文字)：後者按下時打開鍵盤並帶入文字。
    """
    lines = [row for row in text.split("\n") if row.strip()]
    body = [{"type": "text", "text": _limit(row, 2000), "wrap": True, "size": "sm",
             **({"weight": "bold"} if index == 0 else {"color": "#555555"})} for index, row in enumerate(lines)]
    actions = [postback_button(*button) for button in buttons[:12]]
    rows = [{"type": "box", "layout": "horizontal", "spacing": "sm", "contents": actions[start:start + columns]}
            for start in range(0, len(actions), columns)]
    bubble = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body}}
    if rows:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": rows}
    message = {"type": "flex", "altText": _limit(text, 1500), "contents": bubble}
    if quick:
        # 卡片按鈕會保留；Quick Reply 只在最新訊息下方，給記完馬上要修正的人一鍵改。
        message["quickReply"] = _quick_reply(quick)
    return message


def message_text(message: dict) -> str:
    return message.get("text") or message.get("altText", "")


def reply_or_push(token: str | None, recipient: str | None, messages: list[dict]) -> None:
    if not token or not messages:
        return
    try:
        _request("POST", "/message/reply", {"replyToken": token, "messages": messages[:5]})
    except LineError as exc:
        if not exc.reply_unaccepted or not recipient:
            raise
        from app.services.line_delivery import queue_push

        queue_push(recipient, messages[:5], delayed=exc.retryable)
