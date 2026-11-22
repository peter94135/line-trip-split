"""結算卡片與付款人／收款人的還款確認。"""

import hmac
import re
from decimal import Decimal
from urllib.parse import quote, urlencode

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Member, Transfer, Trip, TripMember
from app.services import line, split, trips
from app.services.line_parse import CommandError, money

UPDATED = "結算已更新，請重新傳「結算」。"
MAX_PAYMENTS = 6
MAX_MEMBERS = 60


def _nonce(trip: Trip) -> str:
    secret = get_settings().signing_secret.encode()
    version = f"settlement:{trip.id}:{trip.ledger_version}".encode()
    return hmac.digest(secret, version, "sha256").hex()[:24]


def _text(text: str) -> dict:
    return {"type": "text", "text": text, "wrap": True, "size": "sm"}


def card(session: Session, trip: Trip, *, closing: str = "") -> dict:
    # 成員或支出可能剛透過同一筆交易更新版本。
    session.refresh(trip, attribute_names=["ledger_version"])
    result = trips.total(session, trip)
    roster = trips.people(session, trip.id)
    currency = trip.settle_currency
    header = [f"{trip.name}｜{closing or '結算'}", f"總花費 {money(result.total, currency)}", "成員結餘"]
    missing = len(result.missing_expense_ids) + len(result.missing_transfer_ids)
    if missing:
        header.append(f"{missing} 筆待換算，暫未計入結算")
    for member_id in list(roster)[:MAX_MEMBERS]:
        name = trips.name_of(roster, member_id)[:60]
        balance = result.balances.get(member_id, 0)
        status = f"{'應收' if balance > 0 else '應付'} {money(abs(balance), currency)}" if balance else "已結清"
        header.append(f"{name}　{status}")
    if len(roster) > MAX_MEMBERS:
        header.append(f"其餘 {len(roster) - MAX_MEMBERS} 位成員請開網頁")
    header.append("轉帳建議")

    message = line.card_message("\n".join(header), [])
    contents = message["contents"]["body"]["contents"]
    summary = header[:2]
    if not result.payments:
        status = "目前無須轉帳" if missing else "大家都結清了"
        contents.append(_text(status))
        summary.append(status)

    nonce = _nonce(trip)
    for payment in result.payments[:MAX_PAYMENTS]:
        payer = trips.name_of(roster, payment.from_id)
        recipient = trips.name_of(roster, payment.to_id)
        label = f"{payer} → {recipient}　{money(payment.amount, currency)}"
        summary.append(label)
        parts = [_text(label)]
        if note := roster[payment.to_id][0].payment_note:
            parts.append(_text(f"收款方式：{note}"))
        data = urlencode({"trip": trip.id, "from": payment.from_id, "to": payment.to_id,
                          "amount": format(payment.amount, "f"), "nonce": nonce})
        parts.append({"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
            line.postback_button("我已付", f"settle=paid&{data}"),
            line.postback_button("已收到", f"settle=received&{data}"),
        ]})
        contents.append({"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": parts})
    if len(result.payments) > MAX_PAYMENTS:
        tail = f"其餘 {len(result.payments) - MAX_PAYMENTS} 筆請開網頁"
        contents.append(_text(tail))
        summary.append(tail)
    if liff_id := get_settings().liff_id:
        base = f"https://liff.line.me/{quote(liff_id, safe='')}?trip={trip.id}"
        links = [("查看帳本", "settle")]
        if closing:
            links.append(("查看統計", "stats"))
        message["contents"]["footer"] = {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "height": "sm", "action": {"type": "uri", "label": label, "uri": f"{base}&tab={tab}"}}
            for label, tab in links
        ]}
    message["altText"] = line.card_message("\n".join(summary), [])["altText"]
    return message


def handle(session: Session, group_id: str | None, user_id: str, data: dict[str, str]) -> dict:
    action = data.get("settle")
    ids = [data.get(key, "") for key in ("trip", "from", "to")]
    amount = data.get("amount", "")
    nonce = data.get("nonce", "")
    if (action not in ("paid", "received") or any(not re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in ids)
        or not re.fullmatch(r"[0-9]{1,30}(\.[0-9]{1,2})?", amount) or not re.fullmatch(r"[0-9a-f]{24}", nonce)):
        raise CommandError(UPDATED)
    trip_id, from_id, to_id = map(int, ids)
    if max(trip_id, from_id, to_id) > 9_223_372_036_854_775_807:
        raise CommandError(UPDATED)
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    trip = session.get(Trip, trip_id)
    if trip is None or trip.deleted_at is not None or (group_id and group_id != trip.line_group_id):
        raise CommandError(UPDATED)
    actor = session.exec(select(Member).where(Member.line_user_id == user_id)).first()
    expected = from_id if action == "paid" else to_id
    if actor is None or actor.id != expected or session.get(TripMember, (trip_id, expected)) is None:
        raise CommandError("只有付款人可以標記。" if action == "paid" else "只有收款人可以標記。")

    payment = split.Payment(from_id, to_id, Decimal(amount))
    if not hmac.compare_digest(nonce, _nonce(trip)) or not trips.suggested_payment(session, trip, payment):
        raise CommandError(UPDATED)

    session.add(Transfer(trip_id=trip.id, from_id=from_id, to_id=to_id, amount=float(payment.amount),
                         currency=trip.settle_currency, rate=1, amount_settle=float(payment.amount), created_by=actor.id, source="line_text"))
    session.flush()
    trips.audit(session, trip.id, actor.id, "line", "標記已付" if action == "paid" else "標記已收到",
                f"成員 #{from_id} → #{to_id} {payment.amount}")
    return card(session, trip)
