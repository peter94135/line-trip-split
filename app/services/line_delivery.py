"""持久化群組通知與回覆補送，每次重試使用相同的 LINE retry key。"""

import logging
from datetime import timedelta
from threading import Lock
from uuid import uuid4

from sqlalchemy import delete
from sqlmodel import Session, select

from app.db import get_engine
from app.models import LineDelivery, Trip, utcnow
from app.services import line

log = logging.getLogger(__name__)
_lock = Lock()
RETRY_WINDOW = timedelta(minutes=30)


def enqueue(session: Session, recipient: str, messages: list[dict], *, delayed=False) -> str:
    delivery_id = str(uuid4())
    session.add(LineDelivery(id=delivery_id, recipient=recipient, messages=messages,
                             next_attempt_at=utcnow() + timedelta(seconds=60 if delayed else 0)))
    return delivery_id


def queue_push(recipient: str, messages: list[dict], *, delayed=False) -> None:
    with Session(get_engine()) as session:
        if recipient.startswith("C") and not session.exec(
            select(Trip.id).where(Trip.line_group_id == recipient).limit(1)
        ).first():
            return
        delivery_id = enqueue(session, recipient, messages, delayed=delayed)
        session.commit()
    if not delayed:
        send(delivery_id)


def send(delivery_id: str) -> None:
    with _lock, Session(get_engine()) as session:
        row = session.get(LineDelivery, delivery_id)
        now = utcnow()
        if row is None or row.status != "pending" or row.next_attempt_at > now:
            return
        if now - row.created_at >= RETRY_WINDOW:
            row.status = "failed"
        else:
            try:
                line._request("POST", "/message/push", {"to": row.recipient, "messages": row.messages},
                              retry_key=row.id)
                row.status = "sent"
            except line.LineError as exc:
                log.warning("LINE 推播 %s 失敗：%s", row.id, exc)
                if exc.retryable:
                    row.attempts += 1
                    row.next_attempt_at = now + timedelta(seconds=min(60 * 2 ** (row.attempts - 1), 300))
                else:
                    row.status = "failed"
        if row.status != "pending":
            row.messages = None
        session.commit()


def retry_pending() -> None:
    with Session(get_engine()) as session:
        now = utcnow()
        session.execute(delete(LineDelivery).where(LineDelivery.created_at < now - timedelta(days=7)))
        session.commit()
        ids = session.exec(select(LineDelivery.id).where(
            LineDelivery.status == "pending", LineDelivery.next_attempt_at <= now,
        ).order_by(LineDelivery.next_attempt_at).limit(50)).all()
    for delivery_id in ids:
        send(delivery_id)
