import csv
import io
import json

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Expense, GeminiCall, LineDelivery, LineInbox, Trip, TripMember
from app.services import line, line_chat, line_edit
from app.services.line_parse import CommandError


def test_liff_login_invitation_and_membership_isolation(web):
    client, login, (trip, _, _) = web
    assert client.get("/liff/").status_code == 200
    assert client.get("/assets/liff.js?v=18").status_code == 200
    assert client.get("/api/liff/config").json()["liff_id"] == "test-liff"
    assert client.post("/api/auth/line", json={"id_token": "invalid"}).status_code == 401
    assert client.post("/api/auth/line", json={"id_token": "wrong-audience"}).status_code == 401
    carol = login("Carol")
    assert client.get("/api/trips", headers=carol).json()["trips"] == []
    for path in (f"/api/trips/{trip}", f"/api/trips/{trip}/export.csv"):
        response = client.get(path, headers=carol)
        assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert client.delete(f"/api/trips/{trip}/expenses/1", headers=carol).status_code == 403
    assert client.post("/api/trips/join", headers=carol, json={"code": "NotValid"}).status_code == 404
    for _ in range(2):
        assert client.post("/api/trips/join", headers=carol, json={"code": "AbCd1234"}).json() == {"trip_id": trip}
    visible = client.get(f"/api/trips/{trip}", headers=carol).json()
    assert len(visible["members"]) == 3
    assert "line_user_id" not in str(visible) and "line_group_id" not in visible


def test_closing_trip_on_web_pushes_settlement_to_group_once(web, monkeypatch):
    client, login, (trip_id, alice_id, bob_id) = web
    alice = login("Alice")
    base = f"/api/trips/{trip_id}"
    pushes = []
    monkeypatch.setattr(line, "is_configured", lambda: True)
    monkeypatch.setattr(line, "_request", lambda method, path, payload, *, retry_key=None: pushes.append(payload) or {})
    expense = dict(item="晚餐", spent_on="2026-11-10", amount="100", currency="TWD", category="餐飲",
                   payer_id=alice_id, split_mode="equal",
                   shares=[{"member_id": alice_id, "weight": "1"}, {"member_id": bob_id, "weight": "1"}])
    assert client.post(base + "/expenses", headers=alice, json=expense).status_code == 201
    pushes.clear()

    assert client.post(base + "/close", headers=alice).status_code == 204
    assert len(pushes) == 1 and pushes[0]["to"] == "C-test"
    [message] = pushes[0]["messages"]
    assert message["type"] == "flex"
    assert "Alice 在網頁結束帳本" in message["altText"] and "Bob → Alice　TWD 50" in message["altText"]
    assert message["contents"]["footer"]["contents"][-1]["action"]["uri"].endswith("&tab=stats")
    assert client.post(base + "/close", headers=alice).status_code == 204
    assert len(pushes) == 1


def test_web_record_changes_send_line_cards_to_bound_group_after_commit(web, db_engine, monkeypatch):
    client, login, (trip_id, alice_id, bob_id) = web
    alice = login("Alice")
    base = f"/api/trips/{trip_id}"
    pushes = []

    def request(method, path, payload, *, retry_key=None):
        assert (method, path, payload["to"]) == ("POST", "/message/push", "C-test")
        with Session(db_engine) as session:
            assert session.get(LineDelivery, retry_key) is not None
        assert all(card["type"] == "flex" and "https://" not in str(card) for card in payload["messages"])
        pushes.append(payload["messages"])
        return {}

    def buttons(card):
        return {item["action"]["label"]: item["action"]["data"]
                for row in card["contents"]["footer"]["contents"] for item in row["contents"]}

    monkeypatch.setattr(line, "is_configured", lambda: True)
    monkeypatch.setattr(line, "_request", request)
    expense = dict(item="晚餐", spent_on="2026-11-10", amount="100", currency="TWD", category="餐飲",
                   payer_id=alice_id, split_mode="equal", shares=[{"member_id": alice_id, "weight": "1"}])
    saved = client.post(base + "/expenses", headers=alice, json=expense)
    assert saved.status_code == 201
    entry_id = saved.json()["id"]
    assert pushes[0][0]["altText"].startswith("已記：晚餐 TWD 100｜餐飲\nAlice付")
    assert set(buttons(pushes[0][0])) == {"改分攤", "改付款人", "改類別", "刪除"}
    entry_path = base + f"/expenses/{entry_id}"
    assert client.put(entry_path, headers=alice, json={**expense, "amount": "120"}).status_code == 200
    assert pushes[1][0]["altText"].startswith("Alice 已修改：晚餐 TWD 120")
    assert client.delete(entry_path, headers=alice).status_code == 204
    assert pushes[2][0]["altText"] == "Alice 已刪除「晚餐」。"
    assert buttons(pushes[2][0]) == {"復原": f"exp=undo&id={entry_id}"}
    assert client.delete(entry_path, headers=alice).status_code == 404
    invalid = {"expenses": [expense, {**expense, "payer_id": 999}]}
    assert client.post(base + "/expenses/bulk", headers=alice, json=invalid).status_code == 422
    assert len(pushes) == 3

    monkeypatch.setattr("app.services.fx.rates", lambda: {"USD": .03})
    batch = {"expenses": [{**expense, "item": "早餐", "currency": "USD"},
                          {**expense, "item": "住宿退費", "kind": "refund"}]}
    assert client.post(base + "/expenses/bulk", headers=alice, json=batch).status_code == 201
    assert len(pushes) == 4 and len(pushes[3]) == 2
    assert pushes[3][0]["altText"].startswith("已記：早餐 USD 100")
    assert "退款｜住宿退費" in pushes[3][1]["altText"]
    transfer = {"from_id": bob_id, "to_id": alice_id, "amount": "50", "currency": "TWD"}
    repaid = client.post(base + "/transfers", headers=alice, json=transfer)
    assert repaid.status_code == 201
    assert pushes[4][0]["altText"] == "已記還款：Bob → Alice TWD 50"
    assert client.delete(base + f"/transfers/{repaid.json()['id']}", headers=alice).status_code == 204
    assert pushes[5][0]["altText"] == "Alice 已刪除還款：Bob → Alice TWD 50"

    # 帳本設定關閉群組通知後，網頁改帳不再推播，也不排補送。
    settings = {"name": "京都", "timezone": "Asia/Taipei", "default_currency": "TWD", "settle_currency": "TWD",
                "fixed_rates": {"JPY": "0.2"}}
    assert client.put(base + "/settings", headers=alice, json={**settings, "notify_web_changes": False}).status_code == 204
    assert client.get(base, headers=alice).json()["notify_web_changes"] is False
    assert client.post(base + "/expenses", headers=alice, json=expense).status_code == 201
    assert len(pushes) == 6
    # 沒帶這個欄位的舊版網頁不會改到開關。
    assert client.put(base + "/settings", headers=alice, json=settings).status_code == 204
    assert client.get(base, headers=alice).json()["notify_web_changes"] is False
    assert client.put(base + "/settings", headers=alice, json={**settings, "notify_web_changes": True}).status_code == 204
    assert client.post(base + "/expenses", headers=alice, json=expense).status_code == 201
    assert len(pushes) == 7

    with Session(db_engine) as session:
        private = Trip(name="個人測試", created_by=alice_id)
        session.add(private)
        session.flush()
        private_id = private.id
        session.add(TripMember(trip_id=private_id, member_id=alice_id))
        session.commit()
    assert client.post(f"/api/trips/{private_id}/expenses", headers=alice, json=expense).status_code == 201
    assert len(pushes) == 7


def test_web_expenses_immediately_change_line_settlement_and_export(web, db_engine):
    client, login, (trip, alice_id, bob_id) = web
    alice, bob = login("Alice"), login("Bob")
    base = f"/api/trips/{trip}"
    body = dict(item="=1+1", merchant="測試店", spent_on="2026-11-10", amount="1000", currency="TWD",
                payer_id=alice_id, category="餐飲", split_mode="equal",
                shares=[{"member_id": alice_id, "weight": "1"}, {"member_id": bob_id, "weight": "1"}])
    created = client.post(base + "/expenses", headers=alice, json=body)
    assert created.status_code == 201, created.text
    entry = base + f"/expenses/{created.json()['id']}"
    event = {"type": "message", "source": {"type": "group", "groupId": "C-test", "userId": "U-Alice"},
             "message": {"type": "text", "text": "結算"}}

    def line_settlement():
        with Session(db_engine) as session:
            text = line.message_text(line_chat.handle(session, event)[0])
            session.commit()
            return text

    assert "Bob → Alice　TWD 500" in line_settlement()
    changed = {**body, "amount": "1500", "split_mode": "exact",
               "shares": [{"member_id": alice_id, "amount": "700"}, {"member_id": bob_id, "amount": "700"}]}
    assert client.put(entry, headers=bob, json=changed).status_code == 422
    changed["shares"][1]["amount"] = "800"
    assert client.put(entry, headers=bob, json=changed).status_code == 200
    assert "Bob → Alice　TWD 800" in line_settlement()
    assert client.post(base + "/transfers", headers=bob, json={"from_id": bob_id, "to_id": alice_id, "amount": "200",
                                                              "currency": "TWD"}).status_code == 201
    assert "Bob → Alice　TWD 600" in line_settlement()

    exported = client.get(base + "/export.csv", headers=alice)
    assert exported.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert rows[1][2] == "'=1+1" and rows[2][0] == "還款"
    assert client.delete(entry, headers=bob).status_code == 204
    assert client.get(base, headers=alice).json()["expenses"] == []


def test_statistics_match_rounded_ledger_and_line(web, db_engine, monkeypatch):
    client, login, (trip, alice_id, bob_id) = web
    alice = login("Alice")
    base = f"/api/trips/{trip}"
    monkeypatch.setattr("app.services.fx.rates", lambda: {})
    data = {"spent_on": "2026-11-10", "payer_id": alice_id, "split_mode": "equal",
            "shares": [{"member_id": member_id, "weight": "1"} for member_id in (alice_id, bob_id)]}
    for item, category, amount, currency in [
        ("晚餐", "餐飲", "503", "JPY"), ("火車票", "交通", "50", "TWD"), ("住宿", "住宿", "20", "USD"),
    ]:
        response = client.post(base + "/expenses", headers=alice, json={
            **data, "item": item, "category": category, "amount": amount, "currency": currency,
        })
        assert response.status_code == 201, response.text

    assert client.get(base + "/stats", headers=login("Carol")).status_code == 403
    stats = client.get(base + "/stats", headers=alice).json()
    ledger = client.get(base, headers=alice).json()["settlement"]
    assert stats["total"] == ledger["total"] == "151"
    assert stats["missing_count"] == 1
    assert stats["by_day"] == [{"day": "2026-11-10", "amount": "151"}]
    assert stats["by_category"] == [
        {"category": "餐飲", "amount": "101", "percent": "66.89"},
        {"category": "交通", "amount": "50", "percent": "33.11"},
    ]
    assert stats["by_member_category"]["餐飲"][str(bob_id)] == {"paid": "0", "owed": "50"}
    assert stats["by_member"][str(bob_id)]["owed"] == ledger["owed"][str(bob_id)] == "75"
    event = {"type": "message", "source": {"type": "group", "groupId": "C-test", "userId": "U-Bob"},
             "message": {"type": "text", "text": "統計"}}
    with Session(db_engine) as session:
        text = line_chat.handle(session, event)[0]["text"]
        assert "餐飲　TWD 101（66.89%）" in text and "1 筆缺匯率未計入" in text and "&tab=stats" in text


def test_payment_notes_and_marking_after_close(web, db_engine):
    client, login, (trip, alice_id, bob_id) = web
    alice, bob = login("Alice"), login("Bob")
    base = f"/api/trips/{trip}"
    note_path = base + f"/members/{alice_id}/payment-note"
    note = {"payment_note": "LINE Pay 末碼 1234"}
    assert client.put(note_path, headers=bob, json=note).status_code == 403
    assert client.put(note_path, headers=alice, json={"payment_note": "銀行 123-12345678"}).status_code == 422
    assert client.put(note_path, headers=alice, json=note).status_code == 204
    assert client.post(base + "/expenses", headers=alice, json={
        "item": "晚餐", "spent_on": "2026-11-10", "amount": "1000", "currency": "TWD", "payer_id": alice_id,
        "split_mode": "equal", "shares": [{"member_id": bob_id, "weight": "1"}],
    }).status_code == 201
    assert client.post(base + "/close", headers=alice).status_code == 204
    detail = client.get(base, headers=bob).json()
    assert detail["members"][0]["payment_note"] == note["payment_note"]
    assert note["payment_note"] not in client.get(base + "/export.csv", headers=bob).text
    event = {"type": "message", "source": {"type": "group", "groupId": "C-test", "userId": "U-Bob"},
             "message": {"type": "text", "text": "結算"}}
    with Session(db_engine) as session:
        card = line_chat.handle(session, event)[0]
        assert card["type"] == "flex" and note["payment_note"] in str(card["contents"])

    body = {"from_id": bob_id, "to_id": alice_id, "amount": "1000", "currency": "TWD"}
    assert client.post(base + "/transfers", headers=bob, json=body).status_code == 409
    body["settlement_version"] = detail["settlement_version"]
    assert client.post(base + "/transfers", headers=bob, json={**body, "amount": "999"}).status_code == 409
    assert client.post(base + "/transfers", headers=bob, json=body).status_code == 201
    assert client.post(base + "/transfers", headers=alice, json=body).status_code == 409
    assert client.get(base, headers=bob).json()["settlement"]["payments"] == []


def test_refund_stats_export_and_display_currency(web, monkeypatch):
    client, login, (trip, alice_id, bob_id) = web
    alice = login("Alice")
    base = f"/api/trips/{trip}"
    monkeypatch.setattr("app.services.fx.rates", lambda: {"USD": .03})
    common = {"spent_on": "2026-11-10", "payer_id": alice_id, "split_mode": "equal", "category": "住宿",
              "shares": [{"member_id": bob_id, "weight": "1"}]}
    for item, kind, amount, currency in [
        ("住宿", "expense", "1000", "JPY"), ("住宿退費", "refund", "500", "JPY"), ("清潔費", "expense", "3", "USD"),
    ]:
        assert client.post(base + "/expenses", headers=alice, json={
            **common, "item": item, "kind": kind, "amount": amount, "currency": currency,
        }).status_code == 201
    detail = client.get(base, headers=alice).json()
    assert detail["display_currencies"] == ["JPY", "TWD", "USD"]
    assert detail["settlement"]["owed"][str(bob_id)] == "200"
    assert client.get(base + "/stats", headers=alice).json()["by_category"] == [
        {"category": "住宿", "amount": "200", "percent": "100.00"},
    ]
    jpy = client.get(base + "/settlement-display?currency=JPY", headers=alice).json()
    usd = client.get(base + "/settlement-display?currency=USD", headers=alice).json()
    assert jpy["amounts"] == ["1000"] and jpy["source"] == "帳本固定匯率"
    assert usd["amounts"] == ["6.00"] and usd["source"] == "Rates By Exchange Rate API"
    assert client.get(base + "/settlement-display?currency=EUR", headers=alice).status_code == 422
    assert client.get(base + "/settlement-display?currency=JPY", headers=login("Carol")).status_code == 403
    assert client.get(base, headers=alice).json() == detail
    rows = list(csv.reader(io.StringIO(client.get(base + "/export.csv", headers=alice).content.decode("utf-8-sig"))))
    assert any(row[0] == "退款" and row[8] == "-500.0" and row[10] == "-100.0" for row in rows[1:])


def test_settings_protect_locked_currency_and_historical_member_shares(web):
    client, login, (trip, alice_id, bob_id) = web
    alice, bob = login("Alice"), login("Bob")
    base = f"/api/trips/{trip}"
    body = dict(item="晚餐", spent_on="2026-11-10", amount="1000", currency="JPY", payer_id=alice_id, split_mode="equal",
                shares=[{"member_id": alice_id, "weight": "1"}, {"member_id": bob_id, "weight": "1"}])
    response = client.post(base + "/expenses", headers=alice, json=body)
    assert response.status_code == 201, response.text
    path = base + f"/expenses/{response.json()['id']}"
    settings = dict(name="京都新名稱", timezone="Asia/Tokyo", default_currency="JPY", settle_currency="TWD", fixed_rates={"JPY": ".25"})
    assert client.put(base + "/settings", headers=bob, json=settings).status_code == 403
    assert client.put(base + "/settings", headers=alice, json={**settings, "settle_currency": "USD"}).status_code == 409
    assert client.put(base + "/settings", headers=alice, json=settings).status_code == 204
    membership = base + f"/members/{bob_id}"
    assert client.put(membership, headers=bob, json={"active": False}).status_code == 403
    assert client.put(membership, headers=alice, json={"active": False}).status_code == 204
    assert client.post(base + "/expenses", headers=alice, json=body).status_code == 422
    assert client.put(path, headers=alice, json={**body, "amount": "2000"}).status_code == 200
    detail = client.get(base, headers=alice).json()
    assert detail["name"] == settings["name"] and detail["expenses"][0]["amount_settle"] == 400
    assert client.post(base + "/close", headers=bob).status_code == 403
    assert client.post(base + "/close", headers=alice).status_code == 204
    assert client.get(base, headers=bob).json()["status"] == "closed"
    assert client.post(base + "/expenses", headers=alice, json=body).status_code == 409


def test_reopen_restores_recording_and_enforces_one_open_trip_per_group(web, db_engine):
    client, login, (trip_id, alice_id, _) = web
    alice, bob = login("Alice"), login("Bob")
    path = f"/api/trips/{trip_id}"
    expense = dict(item="晚餐", spent_on="2026-11-10", amount="100", currency="TWD",
                   payer_id=alice_id, split_mode="equal", shares=[{"member_id": alice_id, "weight": "1"}])
    original = client.post(path + "/expenses", headers=alice, json=expense)
    assert original.status_code == 201
    invite_url = client.get(path, headers=alice).json()["invite_url"]
    assert client.post(path + "/close", headers=alice).status_code == 204
    assert client.post(path + "/reopen", headers=bob).status_code == 403
    assert client.post(path + "/reopen", headers=login("Carol")).status_code == 403
    with Session(db_engine) as session:
        newer = Trip(name="下一趟", created_by=alice_id, line_group_id="C-test")
        session.add(newer)
        session.flush()
        newer_id = newer.id
        session.add(TripMember(trip_id=newer_id, member_id=alice_id))
        session.commit()

    conflict = client.post(path + "/reopen", headers=alice)
    assert conflict.status_code == 409 and "先結束" in conflict.json()["detail"]
    assert client.get(path, headers=alice).json()["status"] == "closed"
    assert client.post(f"/api/trips/{newer_id}/close", headers=alice).status_code == 204
    for _ in range(2):
        assert client.post(path + "/reopen", headers=alice).status_code == 204
    detail = client.get(path, headers=bob).json()
    assert detail["status"] == "open" and detail["invite_url"] == invite_url
    assert [row["id"] for row in detail["expenses"]] == [original.json()["id"]]
    assert client.post(path + "/expenses", headers=alice, json={**expense, "item": "早餐"}).status_code == 201


def test_line_open_rechecks_after_web_reopens_during_profile_lookup(web, db_engine, monkeypatch):
    client, login, (trip_id, _, _) = web
    alice = login("Alice")
    path = f"/api/trips/{trip_id}"
    assert client.post(path + "/close", headers=alice).status_code == 204

    def get_profile(user, group):
        assert client.post(path + "/reopen", headers=alice).status_code == 204
        return {"displayName": "Alice"}

    monkeypatch.setattr(line, "get_profile", get_profile)
    event = {"type": "message", "source": {"type": "group", "groupId": "C-test", "userId": "U-Alice"},
             "message": {"type": "text", "text": "開帳本 另一趟"}}
    with Session(db_engine) as session:
        assert "已有進行中的「京都」" in line.message_text(line_chat.handle(session, event)[0])
    assert [row["id"] for row in client.get("/api/trips", headers=alice).json()["trips"]] == [trip_id]


def test_admin_scope_overview_and_review(web, db_engine):
    client, login, _ = web
    with Session(db_engine) as session:
        row = LineInbox(event_key="needs-review", actor="private", status="failed", payload={"type": "message", "text": "private"})
        session.add_all([row, GeminiCall(model="gemini-test", kind="TripParse", input_tokens=10, output_tokens=20)])
        session.flush(); key = row.id
        session.commit()
    assert client.get("/admin").status_code == 200
    assert client.get("/api/admin/overview", headers=login("Alice")).status_code == 401
    assert client.post("/api/admin/login", json={"password": "bad"}).status_code == 401
    token = client.post("/api/admin/login", json={"password": "test-admin-password"}).json()["token"]
    admin = {"Authorization": "Bearer " + token}
    assert client.get("/api/trips", headers=admin).status_code == 401
    data = client.get("/api/admin/overview", headers=admin).json()
    assert data["trips"][0]["name"] == "京都" and data["trips"][0]["members"] >= 1
    assert data["gemini"]["usage"][0]["input_tokens"] == 10 and data["gemini"]["daily"][0]["calls"] >= 1
    assert data["line"]["failed"][0]["type"] == "message" and "private" not in str(data["line"]["failed"])
    assert data["system"]["db_bytes"] > 0
    detail = client.get(f"/api/admin/trips/{data['trips'][0]['id']}", headers=admin).json()
    assert detail["trip"]["name"] == "京都" and "shares" not in detail["settlement"]
    assert client.get("/api/admin/trips/999", headers=admin).status_code == 404
    assert client.post(f"/api/admin/inbox/{key}/review", headers=admin).status_code == 204
    assert client.get("/api/admin/overview", headers=admin).json()["line"]["failed"] == []


def test_delete_closed_trip_hides_it_from_liff_and_line(web, db_engine):
    client, login, (trip, alice_id, _) = web
    alice, bob = login("Alice"), login("Bob")
    path = f"/api/trips/{trip}"
    entry = client.post(path + "/expenses", headers=alice, json={
        "item": "旅館", "spent_on": "2026-11-10", "amount": "1000", "currency": "TWD",
        "payer_id": alice_id, "split_mode": "equal", "shares": [{"member_id": alice_id, "weight": "1"}],
    })
    assert entry.status_code == 201, entry.text
    entry_id = entry.json()["id"]
    assert client.delete(path, headers=alice).status_code == 409
    assert client.post(path + "/close", headers=alice).status_code == 204
    assert client.delete(path, headers=bob).status_code == 403
    assert client.delete(path, headers=alice).status_code == 204
    assert client.get("/api/trips", headers=bob).json()["trips"] == []
    assert client.get(path, headers=alice).status_code == 404
    assert client.get(path + "/export.csv", headers=alice).status_code == 404
    assert client.post("/api/trips/join", headers=alice, json={"code": "AbCd1234"}).status_code == 404
    token = client.post("/api/admin/login", json={"password": "test-admin-password"}).json()["token"]
    admin = {"Authorization": "Bearer " + token}
    # 管理頁仍列出已刪除的帳本，可查明細並還原。
    [listed] = client.get("/api/admin/overview", headers=admin).json()["trips"]
    assert listed["deleted_at"] is not None
    assert client.get(f"/api/admin/trips/{listed['id']}", headers=admin).json()["audit"][0]["action"] == "刪除帳本"

    event = {"type": "message", "source": {"type": "group", "groupId": "C-test", "userId": "U-Alice"},
             "message": {"type": "text", "text": "結算"}}
    with Session(db_engine) as session:
        assert session.get(Expense, entry_id).item == "旅館"
        assert "還沒有帳本" in line_chat.handle(session, event)[0]["text"]
        with pytest.raises(CommandError, match="已失效"):
            line_edit.handle(session, "C-test", "U-Alice", {"id": str(entry_id), "exp": "del"})
    assert client.post(f"/api/admin/trips/{listed['id']}/restore", headers=admin).status_code == 204
    assert client.get(path, headers=alice).status_code == 200


def test_delete_unused_member_preserves_historical_members(web, db_engine):
    client, login, (trip, alice_id, bob_id) = web
    alice, bob = login("Alice"), login("Bob")
    path = f"/api/trips/{trip}"
    people = path + "/members"

    def add(name):
        response = client.post(people, headers=alice, json={"nickname": name, "weight": "1"})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    unused, payer, recipient = [add(name) for name in ("測試旅伴", "付款人", "收款人")]
    assert client.delete(f"{people}/{unused}", headers=bob).status_code == 403
    assert client.delete(f"{people}/{alice_id}", headers=alice).status_code == 409
    assert client.delete(f"{people}/{unused}", headers=alice).status_code == 204
    assert unused not in [member["id"] for member in client.get(path, headers=alice).json()["members"]]

    entry = client.post(path + "/expenses", headers=alice, json={
        "item": "晚餐", "spent_on": "2026-11-10", "amount": "1000", "currency": "TWD",
        "payer_id": payer, "split_mode": "equal", "shares": [{"member_id": bob_id, "weight": "1"}],
    })
    assert entry.status_code == 201, entry.text
    assert client.delete(path + f"/expenses/{entry.json()['id']}", headers=alice).status_code == 204
    with Session(db_engine) as session:
        assert session.get(Expense, entry.json()["id"]).deleted_by == alice_id
    assert client.post(path + "/transfers", headers=alice, json={
        "from_id": alice_id, "to_id": recipient, "amount": "200", "currency": "TWD",
    }).status_code == 201
    members = client.get(path, headers=alice).json()["members"]
    assert not any(m["can_delete"] for m in members)
    for member_id in (payer, bob_id, recipient):
        blocked = client.delete(f"{people}/{member_id}", headers=alice)
        assert blocked.status_code == 409 and "保留歷史帳目" in blocked.json()["detail"]

    closed_unused = add("結束後移除")
    assert client.post(path + "/close", headers=alice).status_code == 204
    assert client.delete(f"{people}/{closed_unused}", headers=alice).status_code == 204


def test_web_ai_drafts_confirm_in_one_batch(web, db_engine, monkeypatch):
    client, login, (trip, alice_id, bob_id) = web
    alice, carol = login("Alice"), login("Carol")
    path = f"/api/trips/{trip}"
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    # 快速格式不呼叫 AI，只產生草稿，還沒入帳。
    drafted = client.post(path + "/ai/parse", headers=alice, json={"text": "晚餐 3000 我和Bob"})
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    [entry] = body["expenses"]
    assert body["source"] == "web" and entry["payer_id"] == alice_id and entry["category"] == "餐飲"
    assert {share["member_id"] for share in entry["shares"]} == {alice_id, bob_id}
    assert client.get(path, headers=alice).json()["expenses"] == []
    assert client.post(path + "/ai/parse", headers=carol, json={"text": "晚餐 3000"}).status_code == 403
    hint = client.post(path + "/ai/parse", headers=alice, json={"text": "幫我看看"}).json()["detail"]
    assert "「晚餐 3000」" in hint and "「記" not in hint
    photo = client.post(path + "/ai/photo", headers={**alice, "Content-Type": "text/plain"}, content=b"x")
    assert photo.status_code == 415
    unavailable = client.post(path + "/ai/photo", headers={**alice, "Content-Type": "image/jpeg"}, content=b"x")
    assert unavailable.status_code == 422 and "AI" in unavailable.json()["detail"]

    second = {**entry, "item": "咖啡", "amount": "120", "category": "雜支"}
    saved = client.post(path + "/expenses/bulk", headers=alice, json={"source": body["source"], "text": "晚餐 3000 我和Bob",
                                                                       "expenses": [entry, second]})
    assert saved.status_code == 201, saved.text
    rows = client.get(path, headers=alice).json()["expenses"]
    assert sorted(row["item"] for row in rows) == ["咖啡", "晚餐"]
    assert next(row for row in rows if row["item"] == "咖啡")["category"] == "雜支"
    # 一批裡有一筆無效時整批不入帳。
    bad = {**entry, "payer_id": 999}
    assert client.post(path + "/expenses/bulk", headers=alice, json={"expenses": [entry, bad]}).status_code == 422
    assert len(client.get(path, headers=alice).json()["expenses"]) == 2
    with Session(db_engine) as session:
        assert {row.source for row in session.exec(select(Expense))} == {"web"}


def test_liff_page_inlines_config_and_versioned_assets_are_immutable(web):
    client, _, _ = web
    page = client.get("/liff/")
    assert page.headers["cache-control"] == "no-store"
    for response in (page, client.get("/admin")):
        directives = dict(part.strip().split(" ", 1) for part in response.headers["Content-Security-Policy"].split(";"))
        assert directives["frame-ancestors"] == "'none'"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert directives["script-src"].split() == [
            "'self'", "https://static.line-scdn.net/liff/", "https://static.line-scdn.net/lui/"]

    config = page.text.split('<script id="liff-config" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(config) == client.get("/api/liff/config").json()
    assert "immutable" in client.get("/assets/liff.js?v=18").headers["cache-control"]
    assert "immutable" not in client.get("/assets/liff.js").headers.get("cache-control", "")


def test_group_profile_names_are_cached_briefly(monkeypatch):
    from app.services import line_chat
    calls = []
    monkeypatch.setattr(line, "get_profile", lambda user, group: calls.append(user) or {"displayName": "Alice"})
    assert line_chat._profile_name("U-Alice", "C-1") == "Alice"
    assert line_chat._profile_name("U-Alice", "C-1") == "Alice"
    assert calls == ["U-Alice"]
    line_chat._profile_cache[("C-1", "U-Alice")] = (line_chat.utcnow() - line_chat.PROFILE_TTL, "Alice")
    assert line_chat._profile_name("U-Alice", "C-1") == "Alice" and len(calls) == 2


def test_default_payer_setting_applies_to_ai_drafts(web, db_engine):
    client, login, (trip_id, alice_id, bob_id) = web
    alice, bob = login("Alice"), login("Bob")
    base = f"/api/trips/{trip_id}"
    settings = {"name": "京都", "timezone": "Asia/Taipei", "default_currency": "TWD", "settle_currency": "TWD",
                "fixed_rates": {"JPY": "0.2"}}
    assert client.get(base, headers=alice).json()["default_payer_id"] is None
    assert client.put(base + "/settings", headers=bob, json={**settings, "default_payer_id": bob_id}).status_code == 403
    assert client.put(base + "/settings", headers=alice, json={**settings, "default_payer_id": 999}).status_code == 422
    assert client.put(base + "/settings", headers=alice, json={**settings, "default_payer_id": bob_id}).status_code == 204
    assert client.get(base, headers=alice).json()["default_payer_id"] == bob_id
    draft = client.post(base + "/ai/parse", headers=alice, json={"text": "晚餐 300"}).json()["expenses"][0]
    assert draft["payer_id"] == bob_id
    mine = client.post(base + "/ai/parse", headers=alice, json={"text": "我付 晚餐 300"}).json()["expenses"][0]
    assert mine["payer_id"] == alice_id
    # 舊版網頁沒帶欄位時不改設定；0 改回記帳的人。
    assert client.put(base + "/settings", headers=alice, json=settings).status_code == 204
    assert client.get(base, headers=alice).json()["default_payer_id"] == bob_id
    assert client.put(base + "/settings", headers=alice, json={**settings, "default_payer_id": 0}).status_code == 204
    assert client.get(base, headers=alice).json()["default_payer_id"] is None


def test_ai_limits_shared_by_text_photo_and_trips(web, db_engine, monkeypatch):
    from datetime import timedelta
    from concurrent.futures import ThreadPoolExecutor
    from app.models import WebAiRequest, utcnow
    from app.services import web_ai

    client, login, (trip_id, alice_id, _) = web
    alice, bob = login("Alice"), login("Bob")
    monkeypatch.setenv("WEB_AI_PER_MINUTE", "1")
    monkeypatch.setenv("WEB_AI_PER_DAY", "2")
    get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(web_ai, "drafts", lambda *args, **kwargs: calls.append(1) or {"expenses": []})
    with Session(db_engine) as session:
        other = Trip(name="另一趟")
        session.add(other)
        session.flush()
        other_id = other.id
        session.add(TripMember(trip_id=other.id, member_id=alice_id))
        session.commit()
    def parse():
        return client.post(f"/api/trips/{trip_id}/ai/parse", headers=alice, json={"text": "晚餐 60"})
    with ThreadPoolExecutor(2) as pool:
        assert sorted(r.status_code for r in pool.map(lambda _: parse(), range(2))) == [200, 429]
    photo = f"/api/trips/{other_id}/ai/photo"
    def upload():
        return client.post(photo, headers={**alice, "Content-Type": "image/jpeg"}, content=b"test")
    blocked = upload()
    assert blocked.status_code == 429 and 0 < int(blocked.headers["retry-after"]) <= 60
    assert client.post(f"/api/trips/{trip_id}/ai/parse", headers=bob, json={"text": "晚餐 60"}).status_code == 200
    def age_requests(hours):
        with Session(db_engine) as session:
            for row in session.exec(select(WebAiRequest).where(WebAiRequest.member_id == alice_id)):
                row.at = utcnow() - timedelta(hours=hours)
            session.commit()
    age_requests(1)
    assert upload().status_code == 200
    age_requests(1)
    assert "24 小時" in parse().json()["detail"]
    age_requests(25)
    assert parse().status_code == 200 and len(calls) == 4
