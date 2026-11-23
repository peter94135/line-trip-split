"""先持久化 LINE Webhook，再依群組或私訊順序處理。"""

import hashlib
import json
import logging
import time
from collections import deque
from datetime import timedelta
from threading import Lock

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import delete, update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.db import get_engine
from app.models import LineInbox, utcnow
from app.services import admin_push, line, line_chat
from app.services.line_parse import CommandError

router = APIRouter()
log = logging.getLogger(__name__)
MAX_BODY = 1024 * 1024
_locks = [Lock() for _ in range(64)]
SIGNATURE_ALERT = 10  # 10 分鐘內簽章錯誤達此次數就通知管理員
_bad_signatures: deque[float] = deque()
_bad_lock = Lock()


def _bad_signature() -> None:
    now = time.monotonic()
    with _bad_lock:
        _bad_signatures.append(now)
        while now - _bad_signatures[0] >= 600:
            _bad_signatures.popleft()
        if len(_bad_signatures) < SIGNATURE_ALERT:
            return
        _bad_signatures.clear()
    admin_push.spawn(admin_push.event, "line_signature", "LINE Webhook 簽章錯誤",
                     f"10 分鐘內收到 {SIGNATURE_ALERT} 次簽章不符的請求。可能有人在嘗試 Webhook，"
                     "或 LINE_CHANNEL_SECRET 和 LINE Developers 的設定不一致。")


def _store_events(events: list[dict]) -> list[str]:
    actors = {}
    with Session(get_engine()) as session:
        for event in events:
            if event.get("mode") == "standby" or not line_chat.interested(event):
                continue
            source = event["source"]
            actor = f"group:{source['groupId']}" if source.get("type") == "group" else f"user:{source['userId']}"
            key = event.get("webhookEventId") or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            session.execute(insert(LineInbox).values(event_key=key, actor=actor, payload=event)
                            .on_conflict_do_nothing(index_elements=["event_key"]))
            actors[actor] = None
        session.commit()
    return list(actors)


def _finish(session: Session, row_id: int, status: str, payload: dict | None = None) -> None:
    session.execute(update(LineInbox).where(LineInbox.id == row_id).values(status=status, payload=payload))
    session.commit()


def _for_review(event: dict) -> dict:
    return {key: value for key, value in event.items() if key != "replyToken"}


def _process_actor(actor: str) -> None:
    with _locks[hash(actor) % len(_locks)]:
        while True:
            with Session(get_engine()) as session:
                row = session.exec(select(LineInbox).where(LineInbox.actor == actor, LineInbox.status == "pending")
                                   .order_by(LineInbox.id).limit(1)).first()
                if row is None:
                    return
                row_id, event = row.id, row.payload or {}
                # 尚未開始處理的事件可安全補做；回覆權杖過期時由 reply_or_push 改排推播。
                _finish(session, row_id, "processing", event)
                try:
                    messages = line_chat.handle(session, event)
                    _finish(session, row_id, "done")
                except CommandError as exc:
                    session.rollback()
                    _finish(session, row_id, "done")
                    messages = line_chat.reply(str(exc), [("查成員", "cmd=members")] if str(exc).startswith("找不到") else None)
                except Exception:
                    session.rollback()
                    _finish(session, row_id, "failed", _for_review(event))
                    log.exception("處理 LINE 事件 #%s 失敗", row_id)
                    messages = line_chat.reply("這則指令處理失敗，請查看明細後再試一次。")
                source = event.get("source") or {}
                try:
                    line.reply_or_push(event.get("replyToken"), source.get("groupId") or source.get("userId"), messages)
                except line.LineError as exc:
                    log.warning("LINE 事件 #%s 回覆失敗：%s", row_id, exc)


def _handle_actors(actors: list[str]) -> None:
    for actor in actors:
        _process_actor(actor)


def resume_inbox() -> None:
    # 中斷事件的原始操作需要核對，避免重啟後再次記帳。
    with Session(get_engine()) as session:
        rows = session.exec(select(LineInbox).where(LineInbox.status == "processing")).all()
        for row in rows:
            row.status, row.payload = "failed", _for_review(row.payload or {})
        session.commit()


def drain_inbox() -> None:
    with Session(get_engine()) as session:
        session.execute(delete(LineInbox).where(LineInbox.status.in_(("done", "failed", "reviewed")),
                                                LineInbox.received_at < utcnow() - timedelta(days=7)))
        session.commit()
        actors = session.exec(select(LineInbox.actor).where(LineInbox.status == "pending").distinct().limit(50)).all()
    _handle_actors(actors)


@router.post("/line/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Webhook 內容太大")
    if not line.verify_signature(bytes(body), request.headers.get("x-line-signature")):
        _bad_signature()
        raise HTTPException(400, "簽章無效")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Webhook 格式無效") from exc
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise HTTPException(400, "Webhook 事件格式無效")
    actors = await run_in_threadpool(_store_events, events) if events else []
    if actors:
        background.add_task(_handle_actors, actors)
    return {"ok": True}
