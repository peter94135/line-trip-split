import httpx
import pytest
from sqlmodel import Session, select

from app.db import init_db
from app.models import LineDelivery, Trip, utcnow
from app.services import line, line_delivery


def test_connect_failure_retries_but_ambiguous_timeout_does_not(monkeypatch):
    monkeypatch.setattr(line, "RETRY_DELAYS", (0, 0))
    attempts = []

    def connect():
        attempts.append(True)
        if len(attempts) < 3:
            raise httpx.ConnectError("DNS")
        return "ok"

    assert line._with_retry(connect) == "ok"
    assert len(attempts) == 3

    def read():
        attempts.append(False)
        raise httpx.ReadTimeout("不確定是否已送到")

    with pytest.raises(httpx.ReadTimeout):
        line._with_retry(read)
    assert attempts == [True, True, True, False]


def test_fallback_push_retries_with_original_key(db_engine, monkeypatch):
    init_db()
    with Session(db_engine) as session:
        session.add(Trip(name="京都", line_group_id="C-trip"))
        session.commit()
    keys = []

    def request(method, path, payload, *, retry_key=None):
        if path == "/message/reply":
            raise line.LineError("reply token expired", reply_unaccepted=True)
        assert path == "/message/push"
        keys.append(retry_key)
        if len(keys) == 1:
            raise line.LineError("網路暫時中斷", retryable=True)
        return {}

    monkeypatch.setattr(line, "_request", request)
    line.reply_or_push("reply", "C-trip", [line.text_message("已記：早餐 $100")])
    with Session(db_engine) as session:
        delivery = session.exec(select(LineDelivery)).one()
        assert delivery.status == "pending"
        delivery.next_attempt_at = utcnow()
        session.commit()
    line_delivery.retry_pending()
    with Session(db_engine) as session:
        assert session.exec(select(LineDelivery)).one().status == "sent"
    assert keys[0] and keys == [keys[0], keys[0]]
