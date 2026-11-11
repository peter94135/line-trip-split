import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import auth
from app.config import get_settings
from app.main import app
from app.models import Expense, LineInbox, Member, Transfer, Trip, utcnow
from app.routers import line_webhook
from app.services import fx, gemini, line, trips

SECRET = "test-channel-secret"
GROUP = "C-trip"
NAMES = {"U-me": "阿傑", "U-ming": "小明", "U-hua": "小華", "U-new": "新朋友"}


def signed(client, events):
    body = json.dumps({"events": events}, ensure_ascii=False).encode()
    signature = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    return client.post("/line/webhook", content=body, headers={"X-Line-Signature": signature})


@pytest.fixture
def bot(db_engine, monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    monkeypatch.setattr(line, "get_profile", lambda user, group: {"displayName": NAMES[user]})
    monkeypatch.setattr(fx, "rates", lambda: {"JPY": 5, "USD": 0.03})
    replies = []
    # Flex 卡片沒有 text 欄位，測試統一用 altText 當作訊息文字。
    monkeypatch.setattr(line, "reply_or_push", lambda token, to, messages: replies.extend(
        {**message, "text": line.message_text(message)} for message in messages))
    numbers = count()
    with TestClient(app) as client:
        def event(text=None, *, user="U-me", group=GROUP, key=None, mentions=(), postback=None, kind="message",
                  image=None, when=None):
            source = {"type": "group", "groupId": group} if group else {"type": "user"}
            if user:
                source["userId"] = user
            value = {"type": kind, "source": source, "webhookEventId": key or str(next(numbers)), "replyToken": "reply"}
            if text is not None:
                value["message"] = {"type": "text", "text": text, "mention": {"mentionees": list(mentions)}}
            if image:
                value["message"] = {"type": "image", "id": image}
            if when:
                value["timestamp"] = int(when.timestamp() * 1000)
            if postback:
                value.update(type="postback", postback={"data": postback})
            return value

        def send(*args, **kwargs):
            replies.clear()
            response = signed(client, [event(*args, **kwargs)])
            assert response.status_code == 200, response.text
            return replies.copy()

        yield client, event, send, replies


def mention(text, label, user, *, bot=False):
    offset = text.index(label)
    return {"type": "user", "index": len(text[:offset].encode("utf-16-le")) // 2,
            "length": len(label.encode("utf-16-le")) // 2, "userId": user, "isSelf": bot}


def buttons(message):
    if message["type"] == "flex":
        return {item["action"]["label"]: item["action"]["data"]
                for row in message["contents"]["footer"]["contents"] for item in row["contents"]}
    return {item["action"]["label"]: item["action"]["data"] for item in message["quickReply"]["items"]}


def test_group_open_reply_invites_liff_user_into_the_same_trip(bot, db_engine, monkeypatch):
    monkeypatch.setenv("LIFF_ID", "test-liff")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "test-login")
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "verify_line_id", lambda _: ("U-ming", "小明"))
    client, _, send, _ = bot
    created = send("開帳本 台南")[0]["text"]
    with Session(db_engine) as session:
        trip = session.exec(select(Trip)).one()
        trip_id, code = trip.id, trip.invite_code
    url = f"https://liff.line.me/test-liff?join={code}"
    assert url in created
    assert url in send("帳本")[0]["text"]
    assert url in send("開帳本 另一趟")[0]["text"]
    login = client.post("/api/auth/line", json={"id_token": "test-member-token"})
    assert login.status_code == 200
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    assert client.get(f"/api/trips/{trip_id}", headers=headers).status_code == 403
    assert client.post("/api/trips/join", headers=headers, json={"code": code}).json() == {"trip_id": trip_id}
    detail = client.get(f"/api/trips/{trip_id}", headers=headers).json()
    assert detail["invite_url"] == url
    assert {member["name"] for member in detail["members"]} == {"阿傑", "小明"}


def test_quick_record_classifies_food_merchant_name(bot):
    _, _, send, _ = bot
    send("開帳本 台南")
    assert "阿信美食 $500｜餐飲" in send("記 阿信美食 500")[0]["text"]


def test_signed_group_commands_and_isolation(bot, db_engine):
    client, event, send, _ = bot
    assert client.post("/line/webhook", json={"events": [event("開帳本 未授權")]}).status_code == 400
    assert signed(client, []).json() == {"ok": True}
    assert "開帳本" in send(kind="join", user=None)[0]["text"]
    assert "已開帳本" in send("開帳本 京都 11/20-11/25 日幣")[0]["text"]
    assert "進行中" in send("帳本")[0]["text"]
    assert send("這是一般群組聊天", user="U-new", key="ignored") == []
    assert "歡迎" in send(kind="memberJoined", user=None)[0]["text"]
    with Session(db_engine) as session:
        trip = session.exec(select(Trip)).one()
        assert (trip.default_currency, trip.settle_currency) == ("JPY", "TWD")
        assert (trip.start_date.month, trip.start_date.day, trip.end_date.day) == (11, 20, 25)
        assert len(trips.people(session, trip.id)) == 1
        assert not session.exec(select(LineInbox).where(LineInbox.event_key == "ignored")).first()

    assert "加入" in send("加入", user="U-ming")[0]["text"]
    assert "小明" in send("成員")[0]["text"]
    assert "¥3,000" in send("記 晚餐 3000", key="dinner")[0]["text"]
    assert send("記 晚餐 3000", key="dinner") == []
    assert "¥4,500" in send("記 ¥4500 拉麵")[0]["text"]
    assert "¥4,500" in send("記 拉麵 4500 日幣")[0]["text"]
    assert "｜交通\n" in (taxi := send("記 計程車 800 我和小明")[0]["text"]) and "分給 2 人" in taxi
    assert "小明付" in send("記 小明付 門票 2400")[0]["text"]
    assert "已加入" in send("加入", user="U-hua")[0]["text"]
    assert "分給 2 人：阿傑、小明" in send("記 酒 1200 除了小華")[0]["text"]
    text = "@旅伴 記 🥘晚餐 3000 @新朋友"
    assert "分給 2 人：阿傑、新朋友" in send(text, mentions=[
        mention(text, "@旅伴", "U-bot", bot=True), mention(text, "@新朋友", "U-new"),
    ])[0]["text"]
    text = "記 午餐 3000 @小華 我不算"
    assert "分給 1 人：小華" in send(text, mentions=[mention(text, "@小華", "U-hua")])[0]["text"]
    multi = send("記 早餐 350、咖啡 120")[0]
    assert "待確認" in multi["text"]
    assert len(send(postback=buttons(multi)["好"])) == 2
    assert "分給 2 人" in send("記 晚餐 2000 小明 1200、小華 800")[0]["text"]
    assert "找不到" in send("記 晚餐 100 我和阿明")[0]["text"]
    assert "合計必須等於" in send("記 晚餐 2000 小明 1200、小華 700")[0]["text"]
    assert "明細" not in send("明細", group="C-other")[0]["text"]
    assert "¥3,000" in send("明細")[0]["text"]
    assert "文字記帳" in send("記")[0]["text"]
    assert "私訊" in send("我的ID")[0]["text"]
    assert "U-me" in send("我的ID", group=None)[0]["text"]
    with Session(db_engine) as session:
        assert len(session.exec(select(Expense)).all()) == 11
        assert len(session.exec(select(Member)).all()) == 4


def test_statistics_commands_only_show_member_data(bot, monkeypatch):
    monkeypatch.setenv("LIFF_ID", "test-liff")
    get_settings.cache_clear()
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    send("記 晚餐 1001")
    assert "請先傳「加入」" in send("統計", user="U-new")[0]["text"]
    assert "餐飲　$1,001" in send("統計")[0]["text"]
    own = send("我的統計", user="U-ming")[0]["text"]
    assert "我的分擔 $501" in own and "$1,001" not in own
    assert "結算明細：https://liff.line.me/test-liff" in send("我欠多少", user="U-ming")[0]["text"]
    closed = send("結束帳本")
    assert len(closed) == 1 and "統計：https://liff.line.me/test-liff" in closed[0]["text"]
    assert "餐飲　$1,001" in send("統計")[0]["text"]


def test_settlement_postback_permissions_version_and_redelivery(bot, db_engine):
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    send("加入", user="U-hua")
    send("記 晚餐 1500")

    def actions():
        message = send("結算")[0]
        assert message["type"] == "flex"
        return {item["action"]["label"]: item["action"]["data"]
                for box in message["contents"]["body"]["contents"] if box["type"] == "box"
                for row in box["contents"] if row["type"] == "box"
                for item in row["contents"] if "from=2" in item["action"]["data"]}

    original = actions()
    assert "只有付款人" in send(postback=original["我已付"], user="U-hua")[0]["text"]
    assert "只有收款人" in send(postback=original["已收到"], user="U-ming")[0]["text"]
    assert "已更新" in send(postback=original["我已付"], user="U-ming", group="C-other")[0]["text"]
    send("結束帳本")
    assert "已更新" in send(postback=original["我已付"], user="U-ming")[0]["text"]
    current = actions()
    result = send(postback=current["已收到"], user="U-me", key="settle-retry")
    assert result[0]["type"] == "flex" and "小明 → 阿傑" not in result[0]["text"]
    assert send(postback=current["已收到"], user="U-me", key="settle-retry") == []
    assert "已更新" in send(postback=current["我已付"], user="U-ming")[0]["text"]
    with Session(db_engine) as session:
        row = session.exec(select(Transfer)).one()
        assert (row.from_id, row.to_id, row.amount, row.created_by) == (2, 1, 500, 1)


def test_line_refund_examples_and_gemini_confirmation(bot, db_engine, monkeypatch):
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    tax = send("記 退稅 3000")[0]["text"]
    assert "退款｜退稅 $3,000" in tax and "阿傑收" in tax
    deposit = send("記 押金退回 5000 小明收")[0]["text"]
    assert "退款｜押金退回 $5,000" in deposit and "小明收" in deposit

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    get_settings.cache_clear()
    monkeypatch.setattr(gemini, "parse", lambda *args, **kwargs: gemini.TripParse.model_validate({
        "task_type": "expense_add", "expenses": [
            {"item": "住宿退款", "kind": "refund", "amount": 800, "payer": "小明", "category": "住宿"},
            {"item": "早餐", "amount": 200},
        ],
    }))
    draft = send("記 住宿退款 800、早餐 200")[0]
    assert "退款｜住宿退款 $800" in draft["text"] and "小明收" in draft["text"]
    send(postback=buttons(draft)["好"])
    with Session(db_engine) as session:
        rows = session.exec(select(Expense).order_by(Expense.id)).all()
        assert [row.kind for row in rows] == ["refund", "refund", "refund", "expense"]
        assert [row.amount for row in rows] == [3000, 5000, 800, 200]
        assert rows[1].payer_id == rows[2].payer_id != rows[0].payer_id


def test_public_fund_uses_transfers_and_returns_surplus(bot, db_engine):
    _, _, send, _ = bot
    send("開帳本 公費")
    for user in ("U-ming", "U-hua", "U-new"):
        send("加入", user=user)
        assert "你交給阿傑公費 $5,000" in send("交公費 阿傑 5000", user=user)[0]["text"]
    send("記 住宿 12000")
    with Session(db_engine) as session:
        trip = session.exec(select(Trip)).one()
        result = trips.total(session, trip)
        assert result.total == 12_000
        assert result.balances == {1: -6000, 2: 2000, 3: 2000, 4: 2000}
        assert {(row.from_id, row.to_id, row.amount) for row in result.payments} == {
            (1, member_id, 2000) for member_id in (2, 3, 4)
        }


def test_repayments_edits_closed_trip_and_private_selection(bot, db_engine):
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    saved = send("記 晚餐 1000")[0]
    assert "小明 → 阿傑　$500" in send("結算")[0]["text"]
    assert "我 → 阿傑　$500" in send("我欠多少", user="U-ming")[0]["text"]
    assert "已記還款" in send("已還 阿傑 500", user="U-ming")[0]["text"]
    assert "大家都結清了" in send("結算")[0]["text"]

    controls = buttons(saved)
    assert "只有這本帳" in send(group="C-other", postback=controls["刪除"])[0]["text"]
    split_picker = send(postback=controls["改分攤"], user="U-ming")[0]
    stale_done = buttons(split_picker)["完成"]
    assert "已失效" in send(postback=stale_done)[0]["text"]
    split_picker = send(postback=buttons(split_picker)["✓ 小明"], user="U-ming")[0]
    updated = send(postback=buttons(split_picker)["完成"], user="U-ming")[0]
    # 修改後回覆最新內容，並保留編輯按鈕。
    assert updated["text"] == "已改分攤：晚餐 $1,000｜餐飲\n阿傑付｜分給 1 人：阿傑" and "改分攤" in buttons(updated)
    assert "已失效" in send(postback=stale_done, user="U-ming")[0]["text"]
    payer = send(postback=controls["改付款人"])[0]
    assert "已改付款人：晚餐 $1,000｜餐飲\n小明付｜分給 1 人：阿傑" == send(postback=buttons(payer)["小明"])[0]["text"]
    category = send(postback=controls["改類別"])[0]
    assert send(postback=buttons(category)["雜支"])[0]["text"].startswith("已改類別：晚餐 $1,000｜雜支\n")
    # 刪除要再確認一次，回覆與資料都記下操作者。
    confirm = send(postback=controls["刪除"])[0]
    assert confirm["text"] == "確定刪除「晚餐 $1,000」？" and list(buttons(confirm)) == ["確定刪除", "取消"]
    assert "沒有支出" not in send("明細")[0]["text"]
    assert send(postback=buttons(confirm)["取消"])[0]["text"].startswith("未刪除：晚餐 $1,000")
    deleted = send(postback=buttons(confirm)["確定刪除"], user="U-ming")[0]
    assert deleted["text"] == "小明 已刪除「晚餐」。" and list(buttons(deleted)) == ["復原"]
    assert "沒有支出" in send("明細")[0]["text"]
    assert send(postback=controls["刪除"])[0]["text"] == "「晚餐」已由小明刪除。"
    assert send(postback=buttons(deleted)["復原"])[0]["text"].startswith("阿傑 已復原：晚餐 $1,000｜雜支\n小明付")

    assert "建立者" in send("結束帳本", user="U-ming")[0]["text"]
    assert "已結束" in send("結束帳本")[0]["text"]
    assert "已結束" in send("記 晚餐 2000")[0]["text"]
    assert "原帳本成員" in send("結算", user="U-new")[0]["text"]
    send("開帳本 大阪")
    send("開帳本 東京", group="C-other")
    assert "請選擇帳本" in send("記 晚餐 100", group=None)[0]["text"]
    assert "已選擇「東京」" in send("帳本 東京", group=None)[0]["text"]
    assert "已記" in send("午餐 100", group=None)[0]["text"]
    with Session(db_engine) as session:
        dinner = session.get(Expense, 1)
        assert dinner.category == "雜支" and dinner.deleted_at is None
        assert dinner.payer_id == session.exec(select(Member).where(Member.line_user_id == "U-ming")).one().id
        assert len(trips.expense_shares(session, dinner.id)) == 1
        assert session.exec(select(Transfer)).one().amount == 500


def test_restart_recovers_pending_and_flags_interrupted_event(bot, db_engine):
    _, event, send, _ = bot
    send("開帳本 京都")
    with Session(db_engine) as session:
        session.add_all([
            LineInbox(event_key="pending", actor=f"group:{GROUP}", payload=event("記 早餐 100")),
            LineInbox(event_key="interrupted", actor=f"group:{GROUP}", status="processing", payload=event("記 午餐 100")),
            # 容器停機超過回覆權杖效期時，尚未開始的事件仍要補記。
            LineInbox(event_key="stale", actor=f"group:{GROUP}", payload=event("記 晚餐 300"),
                      received_at=utcnow() - timedelta(hours=2)),
        ])
        session.commit()
    with TestClient(app):
        line_webhook.drain_inbox()
        line_webhook.drain_inbox()
    with Session(db_engine) as session:
        assert [row.item for row in session.exec(select(Expense).order_by(Expense.id))] == ["早餐", "晚餐"]
        rows = {row.event_key: row for row in session.exec(select(LineInbox))}
        assert rows["pending"].status == rows["stale"].status == "done"
        assert rows["interrupted"].status == "failed"
        assert "replyToken" not in rows["interrupted"].payload


def use_gemini(monkeypatch, parse):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(gemini, "parse", parse)


def test_complex_expense_requires_its_owners_confirmation(bot, db_engine, monkeypatch):
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    send("加入", user="U-hua")
    when = datetime(2026, 11, 10, 16, tzinfo=UTC)
    calls = []

    def parse(text, **context):
        calls.append((text, context))
        amount = 6200 if context["previous"] else 6400
        return gemini.TripParse(task_type="expense_add", expenses=[gemini.SplitItem(
            item="晚餐", amount=amount, currency="JPY", payer="小明", exclude=["阿傑"],
            spent_on="2026-11-10", category="餐飲",
        )])

    use_gemini(monkeypatch, parse)
    assert "已記" in send("記 早餐 100", when=when)[0]["text"]
    assert calls == []
    first = send("記 昨天晚餐小明付了 6400 日幣，阿傑沒去", when=when)[0]
    assert "2026-11-10 晚餐 ¥6,400" in first["text"] and "小明付｜分攤：小明、小華" in first["text"]
    assert calls[0][1]["today"].isoformat() == "2026-11-11"
    assert {member["line_name"] for member in calls[0][1]["members"]} == {"阿傑", "小明", "小華"}
    assert send("好", user="U-ming") == []
    assert "失效" in send(user="U-ming", postback=buttons(first)["好"])[0]["text"]
    with Session(db_engine) as session:
        assert session.exec(select(Expense)).one().item == "早餐"
    second = send("改成 6200 日幣", when=when)[0]
    assert "¥6,200" in second["text"]
    assert calls[-1][1]["previous"][0]["amount"] == "6400.0"
    assert "失效" in send(postback=buttons(first)["好"])[0]["text"]
    assert "已記" in send("好")[0]["text"]
    assert "失效" in send(postback=buttons(second)["好"])[0]["text"]
    with Session(db_engine) as session:
        saved = session.exec(select(Expense).where(Expense.source == "gemini")).one()
        assert (saved.item, saved.amount, saved.currency, saved.spent_on.isoformat()) == ("晚餐", 6200, "JPY", "2026-11-10")
        assert len(trips.expense_shares(session, saved.id)) == 2


def test_gemini_multi_can_cancel_and_refuses_unknown_names(bot, db_engine, monkeypatch):
    _, _, send, _ = bot
    send("開帳本 京都")
    drafts = [gemini.SplitItem(item=item, amount=amount) for item, amount in (
        ("早餐", 350), ("咖啡", 120), ("伴手禮", 1800),
    )]
    calls = []

    def parse(text, **context):
        calls.append(text)
        return gemini.TripParse(task_type="expense_add", expenses=drafts)

    use_gemini(monkeypatch, parse)
    command = "記 早餐 350、咖啡 120、伴手禮 1800"
    assert "3 筆" in send(command)[0]["text"]
    assert len(calls) == 1
    assert "已取消" in send("取消")[0]["text"]
    assert send("好") == []
    with Session(db_engine) as session:
        assert not session.exec(select(Expense)).all()
    assert "3 筆" in send(command)[0]["text"]
    assert len(send("好")) == 3
    drafts[0].payer = "無名旅客"
    assert "找不到「無名旅客」" in send(command)[0]["text"]
    assert send("好") == []
    with Session(db_engine) as session:
        assert [row.amount for row in session.exec(select(Expense).order_by(Expense.id))] == [350, 120, 1800]


def test_photo_windows_only_download_requested_own_images(bot, db_engine, monkeypatch):
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    when = datetime(2026, 11, 10, 12, tzinfo=UTC)
    downloads = []

    def download(message_id, *, max_bytes):
        downloads.append(message_id)
        return message_id.encode(), "image/jpeg"

    def parse(text, *, image, **context):
        if image[0] == b"blurry":
            return gemini.TripParse(task_type="unknown", clarification="金額看不清楚，請重拍收據。")
        return gemini.TripParse(task_type="expense_add", expenses=[gemini.SplitItem(
            item="車站餐廳", merchant="車站餐廳", amount=2180, currency="JPY",
        )])

    use_gemini(monkeypatch, parse)
    monkeypatch.setattr(line, "get_content", download)
    assert send(image="expired", when=when) == []
    assert "2 分鐘" in send("記 這張", when=when + timedelta(minutes=6))[0]["text"]
    assert send(image="too-late", when=when + timedelta(minutes=9)) == []
    assert "2 分鐘" in send("記 這張", user="U-ming", when=when + timedelta(minutes=10))[0]["text"]
    assert downloads == []
    first = send("記 這張", when=when + timedelta(minutes=10))[0]
    assert "待確認" in first["text"] and "¥2,180 (JPY)｜餐飲" in first["text"]
    assert downloads == ["too-late"]
    with Session(db_engine) as session:
        assert not session.exec(select(Expense)).all()
    assert "已記" in send(postback=buttons(first)["好"])[0]["text"]
    assert "2 分鐘" in send("記", when=when + timedelta(minutes=15))[0]["text"]
    assert "待確認" in send(image="on-time", when=when + timedelta(minutes=16))[0]["text"]
    assert "已取消" in send("取消")[0]["text"]
    assert "2 分鐘" in send("記", when=when + timedelta(minutes=20))[0]["text"]
    assert "看不清楚" in send(image="blurry", when=when + timedelta(minutes=21))[0]["text"]
    assert downloads == ["too-late", "on-time", "blurry"]
    with Session(db_engine) as session:
        saved = session.exec(select(Expense)).one()
        assert (saved.merchant, saved.source, saved.amount) == ("車站餐廳", "line_photo", 2180)


def test_record_and_draft_cards_keep_buttons(bot):
    _, _, send, _ = bot
    send("開帳本 京都")
    send("加入", user="U-ming")
    saved = send("記 晚餐 1000")[0]
    # 按鈕放在 Flex 卡片內，不會像 Quick Reply 一樣在下一則訊息後消失。
    assert saved["type"] == "flex" and "quickReply" not in saved
    assert saved["altText"].startswith("已記：晚餐") and "｜餐飲" in saved["altText"]
    assert list(buttons(saved)) == ["改分攤", "改付款人", "改類別", "刪除"]
    confirm = send(postback=buttons(saved)["刪除"])[0]
    assert confirm["type"] == "flex" and list(buttons(confirm)) == ["確定刪除", "取消"]
    deleted = send(postback=buttons(confirm)["確定刪除"])[0]
    assert deleted["type"] == "flex" and list(buttons(deleted)) == ["復原"]

    # 待確認卡片只用暫時的 Quick Reply，不附操作說明。
    card = send("記 早餐 350、咖啡 120")[0]
    assert card["type"] == "text" and "請回" not in card["text"]
    actions = [item["action"] for item in card["quickReply"]["items"]]
    assert [action["label"] for action in actions] == ["好", "取消", "改成"]
    assert actions[2]["inputOption"] == "openKeyboard" and actions[2]["fillInText"] == "改成 "
    assert send(postback=actions[2]["data"]) == []
    assert "已記" in send(postback=actions[0]["data"])[0]["text"]
