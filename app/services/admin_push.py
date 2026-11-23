"""管理頁的 Web Push 通知：異常開始與恢復時通知架設者，不用 LINE 推播額度。"""

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.models import AdminPushSubscription, Setting

log = logging.getLogger(__name__)
REMIND_S = 3600  # 同一類通知最多每小時一次
_key_lock = threading.Lock()
_state_lock = threading.Lock()


def _key_path() -> Path:
    return get_settings().data_dir / ".vapid_private.pem"


def _vapid() -> Vapid02:
    path = _key_path()
    with _key_lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Vapid02()
            key.generate_keys()
            temporary = path.with_name(path.name + ".tmp")
            key.save_key(str(temporary))
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        return Vapid02.from_file(str(path))


def public_key() -> str:
    raw = _vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _subject() -> str:
    # Apple 要求 https 網址或 mailto；沒有公開網址時用保留網域。
    base = get_settings().public_base_url.strip().rstrip("/")
    return base if base.startswith("https://") else "mailto:admin@example.invalid"


def subscription_count(session: Session) -> int:
    return len(session.exec(select(AdminPushSubscription.endpoint)).all())


def _deliver(payload: dict) -> int:
    """送到所有訂閱的裝置，回傳成功數。裝置已取消訂閱時一併刪除。"""
    with Session(get_engine()) as session:
        rows = session.exec(select(AdminPushSubscription)).all()
        if not rows:
            return 0
        key, claims, sent = _vapid(), {"sub": _subject()}, 0
        for row in rows:
            info = {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}
            try:
                # claims 每次都複製一份，pywebpush 會在裡面補上 aud 與 exp。
                webpush(info, json.dumps(payload, ensure_ascii=False), vapid_private_key=key,
                        vapid_claims=dict(claims), ttl=86400, timeout=10)
                sent += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    session.delete(row)
                else:
                    log.warning("管理頁通知送出失敗（%s）：%s", status, exc)
            except Exception as exc:  # 網路錯誤不能讓排程中斷
                log.warning("管理頁通知送出失敗：%s", exc)
        session.commit()
        return sent


def send(title: str, body: str, *, tag: str, url: str = "/admin") -> int:
    return _deliver({"title": title, "body": body, "tag": tag, "url": url})


def _state(session: Session, key: str) -> dict:
    row = session.get(Setting, f"admin_alert:{key}")
    return json.loads(row.value) if row else {}


def _save(session: Session, key: str, value: dict) -> None:
    session.merge(Setting(key=f"admin_alert:{key}", value=json.dumps(value, ensure_ascii=False)))


def condition(key: str, count: int, title: str, body: str, *, recovered: str, url: str = "/admin") -> None:
    """持續性的異常：開始時通知；仍在時數量增加且過了一小時才再提醒；結束時通知已恢復。"""
    with _state_lock, Session(get_engine()) as session:
        state = _state(session, key)
        now = time.time()
        payload = None
        if count > 0:
            fresh = not state.get("active")
            if fresh or (count > state.get("count", 0) and now - state.get("sent_at", 0) >= REMIND_S):
                payload = (title, body)
                state["sent_at"] = now
            if fresh:
                state["since"] = now
            state.update(active=True, count=count, title=title, body=body)
        elif state.get("active"):
            payload = (f"已恢復：{title}", recovered)
            state = {"active": False, "sent_at": state.get("sent_at", 0)}
        else:
            return
        _save(session, key, state)
        session.commit()
    if payload:
        send(*payload, tag=key, url=url)


def event(key: str, title: str, body: str, *, url: str = "/admin") -> None:
    """單次事件（例如有人一直猜密碼）：同一類每小時最多通知一次。"""
    with _state_lock, Session(get_engine()) as session:
        state = _state(session, key)
        now = time.time()
        if now - state.get("sent_at", 0) < REMIND_S:
            return
        _save(session, key, {"sent_at": now, "title": title, "body": body})
        session.commit()
    send(title, body, tag=key, url=url)


def spawn(function, *args, **kwargs) -> None:
    """在請求處理中觸發的通知改到背景執行，不拖慢回應。測試會換成直接呼叫。"""
    def run() -> None:
        try:
            function(*args, **kwargs)
        except Exception:
            log.exception("管理頁通知處理失敗")
    threading.Thread(target=run, daemon=True).start()


def active_alerts(session: Session) -> list[dict]:
    rows = session.exec(select(Setting).where(Setting.key.startswith("admin_alert:", autoescape=True))).all()
    result = []
    for row in rows:
        state = json.loads(row.value)
        if state.get("active"):
            result.append({"key": row.key.removeprefix("admin_alert:"), "title": state.get("title"),
                           "body": state.get("body"), "since": state.get("since")})
    return sorted(result, key=lambda item: item["since"] or 0)
