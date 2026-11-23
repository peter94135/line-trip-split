import os
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException
from sqlmodel import Session, select

from app import auth
from app.config import get_settings
from app.db import init_db
from app.models import AdminPushSubscription, LineInbox
from app.routers import line_webhook
from app.services import admin_alerts, admin_push, admin_report, line

SUBSCRIPTION = {"endpoint": "https://push.example.test/abc", "expirationTime": None,
                "keys": {"p256dh": "BPublicKey", "auth": "secret"}}


@pytest.fixture
def pushed(db_engine, monkeypatch):
    init_db()
    sent = []
    monkeypatch.setattr(admin_push, "_deliver", lambda payload: sent.append(payload) or 1)
    monkeypatch.setattr(admin_push, "spawn", lambda function, *args, **kwargs: function(*args, **kwargs))
    return sent


def admin(client):
    return {"Authorization": "Bearer " + client.post("/api/admin/login", json={"password": "test-admin-password"}).json()["token"]}


def test_subscribe_requires_admin_and_key_is_private(web, db_engine):
    client, _, _ = web
    assert client.get("/api/admin/push").status_code == 401
    headers = admin(client)
    status = client.get("/api/admin/push", headers=headers).json()
    assert len(status["public_key"]) == 87 and status["devices"] == 0
    key_file = db_engine.url.database.rsplit("/", 1)[0] + "/.vapid_private.pem"
    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    # 重新讀取同一把金鑰，瀏覽器的訂閱才不會失效。
    assert client.get("/api/admin/push", headers=headers).json()["public_key"] == status["public_key"]
    assert client.post("/api/admin/push/subscribe", headers=headers,
                       json={**SUBSCRIPTION, "endpoint": "http://insecure.test"}).status_code == 422
    for _ in range(2):
        assert client.post("/api/admin/push/subscribe", headers=headers, json=SUBSCRIPTION).status_code == 204
    assert client.get("/api/admin/overview", headers=headers).json()["push_devices"] == 1
    assert client.post("/api/admin/push/unsubscribe", headers=headers,
                       json={"endpoint": SUBSCRIPTION["endpoint"]}).status_code == 204
    assert client.post("/api/admin/push/test", headers=headers).status_code == 409


def test_gone_subscriptions_are_removed(web, db_engine, monkeypatch):
    client, _, _ = web
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://trip.example.test/")
    get_settings.cache_clear()
    headers = admin(client)
    client.post("/api/admin/push/subscribe", headers=headers, json=SUBSCRIPTION)
    client.post("/api/admin/push/subscribe", headers=headers, json={**SUBSCRIPTION, "endpoint": "https://push.example.test/ok"})
    calls = []

    def webpush(info, data, **kwargs):
        calls.append((info["endpoint"], kwargs["vapid_claims"]["sub"]))
        if info["endpoint"].endswith("abc"):
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))

    monkeypatch.setattr(admin_push, "webpush", webpush)
    assert client.post("/api/admin/push/test", headers=headers).json() == {"sent": 1}
    assert {sub for _, sub in calls} == {"https://trip.example.test"}
    with Session(db_engine) as session:
        assert [row.endpoint for row in session.exec(select(AdminPushSubscription))] == ["https://push.example.test/ok"]


def test_condition_notifies_start_reminder_and_recovery(db_engine, pushed, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(admin_push.time, "time", lambda: clock[0])
    notify = lambda count: admin_push.condition("x", count, "標題", f"{count} 筆", recovered="好了")
    notify(1)
    notify(1)
    notify(2)  # 數量增加但未滿一小時，不再提醒
    assert [item["body"] for item in pushed] == ["1 筆"]
    clock[0] += 3600
    notify(2)
    notify(3)
    assert [item["body"] for item in pushed] == ["1 筆", "3 筆"]
    with Session(db_engine) as session:
        assert [item["body"] for item in admin_push.active_alerts(session)] == ["3 筆"]
    notify(0)
    notify(0)
    assert pushed[-1] == {"title": "已恢復：標題", "body": "好了", "tag": "x", "url": "/admin"}
    assert len(pushed) == 3
    with Session(db_engine) as session:
        assert admin_push.active_alerts(session) == []


def test_check_reports_failed_events_and_quota(db_engine, pushed, monkeypatch):
    monkeypatch.setattr(line, "is_configured", lambda: True)
    monkeypatch.setattr(admin_report, "line_quota", lambda: {"type": "limited", "limit": 200, "used": 170})
    with Session(db_engine) as session:
        session.add(LineInbox(event_key="e1", actor="group:C", status="failed", payload={}))
        session.commit()
    admin_alerts.check()
    assert {item["tag"] for item in pushed} == {"line_failed", "quota_80"}
    assert "85%" in next(item["body"] for item in pushed if item["tag"] == "quota_80")
    pushed.clear()
    admin_alerts.check()
    assert pushed == []
    with Session(db_engine) as session:
        session.exec(select(LineInbox)).one().status = "reviewed"
        session.commit()
    admin_alerts.check()
    assert [item["title"] for item in pushed] == ["已恢復：LINE 事件待核對"]


def test_repeated_admin_login_failures_alert_once(web, pushed):
    client, _, _ = web
    auth._admin_failures.clear()
    for _ in range(10):
        client.post("/api/admin/login", json={"password": "wrong"})
    auth._admin_failures.clear()
    assert [item["tag"] for item in pushed] == ["admin_login"]
    assert "password" not in str(pushed) and "wrong" not in str(pushed)


def test_bad_webhook_signatures_alert(web, pushed):
    client, _, _ = web
    line_webhook._bad_signatures.clear()
    for _ in range(line_webhook.SIGNATURE_ALERT):
        assert client.post("/line/webhook", content=b"{}", headers={"x-line-signature": "bad"}).status_code == 400
    assert [item["tag"] for item in pushed] == ["line_signature"]


def test_admin_service_worker_and_manifest(web):
    client, _, _ = web
    worker = client.get("/admin-sw.js")
    assert worker.status_code == 200 and "showNotification" in worker.text
    assert worker.headers["cache-control"] == "no-store"
    manifest = client.get("/admin.webmanifest")
    assert manifest.json()["start_url"] == "/admin"
    assert manifest.headers["content-type"].startswith("application/manifest+json")
