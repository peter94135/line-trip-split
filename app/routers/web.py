"""LIFF 與管理頁的 API。帳本資料沿用 LINE 的 SQLite 資料與分攤服務。"""

import csv
from collections.abc import Iterable
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app import auth, web_inputs as inputs
from app.config import get_settings
from app.models import Expense, ExpenseShare, GeminiCall, LineDelivery, LineInbox, Member, Transfer, Trip, TripMember, utcnow
from app.services import (
    admin_report, fx, gemini, line, line_chat, line_delivery, line_edit, split, trip_stats, trips, web_ai,
)
from app.services.line_parse import CURRENCIES, CommandError, money

router = APIRouter(prefix="/api")
MemberUser = Annotated[Member, Depends(auth.require_member)]
AdminUser = Annotated[None, Depends(auth.require_admin)]
RowId = Annotated[int, Path(gt=0, le=9_223_372_036_854_775_807)]
_TRIP_FIELDS = {"id", "name", "status", "start_date", "end_date", "timezone", "default_currency", "settle_currency"}
_EXPENSE_FIELDS = {"id", "spent_on", "item", "kind", "merchant", "category", "amount", "currency", "amount_settle", "payer_id", "split_mode"}
_TRANSFER_FIELDS = {"id", "from_id", "to_id", "amount", "currency", "amount_settle", "created_at"}


def _trip(session: Session, trip_id: int, member: Member) -> Trip:
    if not session.get(TripMember, (trip_id, member.id)):
        raise HTTPException(403, "你不是這本帳的成員")
    trip = session.get(Trip, trip_id)
    if trip is None or trip.deleted_at is not None:
        raise HTTPException(404, "找不到這本帳")
    return trip


def _organizer(trip: Trip, member: Member) -> bool:
    return trip.created_by == member.id or member.line_user_id == get_settings().line_admin_user_id


def _require_organizer(trip: Trip, member: Member) -> None:
    if not _organizer(trip, member):
        raise HTTPException(403, "請由帳本建立者或管理員操作")


def _begin(session: Session) -> None:
    # LINE 和網頁共用同一把 SQLite 寫入鎖，取得後重新讀取有關狀態。
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    session.expire_all()


def _open(trip: Trip) -> None:
    if trip.status != "open":
        raise HTTPException(409, "這本帳已結束")


def _expense(session: Session, trip: Trip, expense_id: int) -> Expense:
    entry = session.get(Expense, expense_id)
    if entry is None or entry.trip_id != trip.id or entry.deleted_at is not None:
        raise HTTPException(404, "找不到這筆支出")
    return entry


def _has_records(session: Session, trip_id: int) -> bool:
    return any(session.exec(select(model.id).where(model.trip_id == trip_id).limit(1)).first() is not None
               for model in (Expense, Transfer))


@router.get("/liff/config")
def liff_config() -> dict:
    settings = get_settings()
    return {"liff_id": settings.liff_id if settings.line_login_channel_id else "",
            "categories": trips.CATEGORIES, "currencies": {key: value[1] for key, value in CURRENCIES.items()},
            "ai": bool(settings.gemini_api_key)}


@router.post("/auth/line")
def line_login(body: inputs.LineLogin, session: auth.Database) -> dict:
    user_id, name = auth.verify_line_id(body.id_token)
    member = trips.ensure_member(session, user_id, name)
    member_id = member.id
    session.commit()
    return {"token": auth.issue_token(str(member_id), "member")}


@router.get("/trips")
def list_trips(session: auth.Database, member: MemberUser) -> dict:
    rows = session.exec(select(Trip).join(TripMember, TripMember.trip_id == Trip.id)
                        .where(TripMember.member_id == member.id, Trip.deleted_at.is_(None))
                        .order_by((Trip.status == "open").desc(), Trip.id.desc()))
    return {"member_id": member.id, "trips": [trip.model_dump(include=_TRIP_FIELDS) for trip in rows]}


@router.post("/trips/join")
def join_trip(body: inputs.JoinTrip, session: auth.Database, member: MemberUser) -> dict:
    _begin(session)
    trip = session.exec(select(Trip).where(Trip.invite_code == body.code, Trip.deleted_at.is_(None))).first()
    if trip is None or (trip.status != "open" and not session.get(TripMember, (trip.id, member.id))):
        raise HTTPException(404, "這個邀請連結無效或帳本已結束")
    trip_id = trip.id
    trips.enroll(session, trip_id, member.id)
    session.commit()
    return {"trip_id": trip_id}


@router.get("/trips/{trip_id}")
def trip_detail(trip_id: RowId, session: auth.Database, member: MemberUser) -> dict:
    session.connection().exec_driver_sql("BEGIN")
    trip = _trip(session, trip_id, member)
    roster = trips.people(session, trip_id)
    shares = trips.shares_by_expense(session, trip_id)
    expenses = trips.expenses(session, trip_id)
    transfers = trips.transfers(session, trip_id)
    return {
        **trip.model_dump(include=_TRIP_FIELDS), "fixed_rates": trip.fixed_rates or {},
        "can_manage": _organizer(trip, member), "has_records": _has_records(session, trip_id),
        "settlement_version": trip.ledger_version,
        "line_group": bool(trip.line_group_id), "notify_web_changes": trip.notify_web_changes,
        "default_payer_id": trips.default_payer(trip, roster, 0) or None,
        "display_currencies": sorted({trip.settle_currency, *(row.currency for row in expenses),
                                      *(row.currency for row in transfers)}),
        "today": utcnow().astimezone(ZoneInfo(trip.timezone)).date(),
        "invite_url": trips.invite_url(trip, get_settings().liff_id) if trip.status == "open" else None,
        "members": [dict(id=key, name=trips.name_of(roster, key), nickname=membership.nickname,
                         weight=membership.weight, active=membership.active, organizer=key == trip.created_by,
                         payment_note=membership.payment_note)
                    for key, (membership, _) in roster.items()],
        "expenses": [dict(**row.model_dump(include=_EXPENSE_FIELDS),
                          shares=[share.model_dump(exclude={"expense_id"}) for share in shares.get(row.id, ())])
                     for row in expenses],
        "transfers": [row.model_dump(include=_TRANSFER_FIELDS) for row in transfers],
        "settlement": asdict(split.settlement(expenses, shares, transfers, trip.settle_currency)),
    }


@router.get("/trips/{trip_id}/stats")
def stats(trip_id: RowId, session: auth.Database, member: MemberUser) -> dict:
    session.connection().exec_driver_sql("BEGIN")
    return trip_stats.calculate(session, _trip(session, trip_id, member))


@router.get("/trips/{trip_id}/settlement-display")
def settlement_display(trip_id: RowId, currency: inputs.Currency, session: auth.Database, member: MemberUser) -> dict:
    session.connection().exec_driver_sql("BEGIN")
    trip = _trip(session, trip_id, member)
    expenses = trips.expenses(session, trip_id)
    transfers = trips.transfers(session, trip_id)
    if currency != trip.settle_currency and not any(row.currency == currency for row in (*expenses, *transfers)):
        raise HTTPException(422, "請選擇帳本已使用的幣別")

    result = split.settlement(expenses, trips.shares_by_expense(session, trip_id), transfers, trip.settle_currency)
    rate, _ = split.convert(1, currency, trip.settle_currency, fixed_rates=trip.fixed_rates)
    source, source_url = ("帳本固定匯率", None) if currency != trip.settle_currency else (None, None)
    if rate is None:
        rate, _ = split.convert(1, currency, trip.settle_currency, rates=fx.rates())
        source, source_url = fx.ATTRIBUTION, fx.ATTRIBUTION_URL
    if rate is None:
        raise HTTPException(409, "目前查不到這個幣別的匯率")
    unit = Decimal("1") if currency in split.ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
    return {"currency": currency, "settlement_version": trip.ledger_version, "source": source, "source_url": source_url,
            "amounts": [(row.amount / rate).quantize(unit, rounding=ROUND_HALF_UP) for row in result.payments]}


def _quote(trip: Trip, amount: Decimal, currency: str, entry: Expense | None = None):
    if entry is not None and entry.currency == currency and entry.rate is not None:
        return entry.rate, float(amount * Decimal(str(entry.rate)))
    return trips.quote_amount(trip, amount, currency)


def _build_shares(body: inputs.ExpenseInput, roster: dict, previous_shares: set[int] = frozenset()) -> list:
    if body.payer_id not in roster:
        raise HTTPException(422, "付款人必須是帳本成員")
    # 原有分攤可以保留已停用的成員，但新支出不能新增該成員的分攤。
    members = [membership.model_copy(update={"active": True}) if key in previous_shares and not membership.active
               else membership for key, (membership, _) in roster.items()]
    exact = {row.member_id: row.amount for row in body.shares} if body.split_mode == "exact" else None
    try:
        return split.build_shares(body.amount, body.payer_id, members, exact=exact,
                                  include=None if exact else [row.member_id for row in body.shares],
                                  weights={row.member_id: row.weight for row in body.shares if row.weight is not None})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _notify_group(session: Session, background: BackgroundTasks, trip: Trip, messages: Iterable[dict]) -> None:
    if not trip.line_group_id or not trip.notify_web_changes or not line.is_configured():
        return
    # 通知和帳目一起存檔，送出在回覆 HTTP 後，服務重啟時也能補送。
    delivery_id = line_delivery.enqueue(session, trip.line_group_id, list(messages))
    background.add_task(line_delivery.send, delivery_id)


def _notify_expenses(session: Session, background: BackgroundTasks, trip: Trip, entries: list[Expense],
                     roster: dict, title: str) -> None:
    _notify_group(session, background, trip, (
        line_edit.expense_card(session, trip, entry, roster, title, show_market_conversion=False,
                              quick=False) for entry in entries
    ))


def _save_expense(session: Session, member: Member, trip_id: int, body: inputs.ExpenseInput,
                  expense_id: int | None = None, *, background: BackgroundTasks) -> dict:
    trip = _trip(session, trip_id, member)
    if expense_id is None:
        _open(trip)
    entry = _expense(session, trip, expense_id) if expense_id is not None else None
    settle_currency = trip.settle_currency
    previous_quote = (entry.currency, entry.rate) if entry is not None else None
    quoted = _quote(trip, body.amount, body.currency, entry)
    _begin(session)
    trip = _trip(session, trip_id, member)
    if expense_id is None:
        _open(trip)
    entry = _expense(session, trip, expense_id) if expense_id is not None else None
    if trip.settle_currency != settle_currency or (entry is not None and previous_quote != (entry.currency, entry.rate)):
        raise HTTPException(409, "幣別或匯率已有變動，請重新整理帳本")
    roster = trips.people(session, trip_id)
    previous_shares = {row.member_id for row in trips.expense_shares(session, entry.id)} if entry else set()
    shares = _build_shares(body, roster, previous_shares)
    if entry is None:
        entry = trips.record_expense(session, trip_id, item=body.item, amount=body.amount, currency=body.currency,
                                     quote=quoted, spent_on=body.spent_on, payer_id=body.payer_id, creator_id=member.id,
                                     shares=shares, source_text=None, source="web", merchant=body.merchant or None, kind=body.kind)
    else:
        trips.audit(session, trip_id, member.id, "web", "修改支出", f"#{entry.id} {entry.item}")
        entry.item, entry.merchant, entry.spent_on = body.item, body.merchant or None, body.spent_on
        if "kind" in body.model_fields_set:
            entry.kind = body.kind
        entry.amount, entry.currency, entry.payer_id = float(body.amount), body.currency, body.payer_id
        entry.rate, entry.amount_settle = quoted
        trips.replace_shares(session, entry, shares)
    if body.category != entry.category and body.category != "待分類":
        trips.set_category(session, entry, body.category)
    else:
        entry.category, entry.category_source = body.category, "user"
    key = entry.id
    title = "已記" if expense_id is None else f"{trips.name_of(roster, member.id)} 已修改"
    _notify_expenses(session, background, trip, [entry], roster, title)
    session.commit()
    return {"id": key}


@router.post("/trips/{trip_id}/expenses", status_code=201)
def add_expense(trip_id: RowId, body: inputs.ExpenseInput, session: auth.Database, member: MemberUser,
                background: BackgroundTasks) -> dict:
    return _save_expense(session, member, trip_id, body, background=background)


@router.put("/trips/{trip_id}/expenses/{expense_id}")
def edit_expense(trip_id: RowId, expense_id: RowId, body: inputs.ExpenseInput, session: auth.Database, member: MemberUser,
                 background: BackgroundTasks) -> dict:
    return _save_expense(session, member, trip_id, body, expense_id, background=background)


@router.delete("/trips/{trip_id}/expenses/{expense_id}", status_code=204)
def delete_expense(trip_id: RowId, expense_id: RowId, session: auth.Database, member: MemberUser,
                   background: BackgroundTasks) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    entry = _expense(session, trip, expense_id)
    entry.deleted_at, entry.deleted_by = utcnow(), member.id
    trips.audit(session, trip_id, member.id, "web", "刪除支出", f"#{entry.id} {entry.item}")
    roster = trips.people(session, trip_id)
    _notify_group(session, background, trip, [line_edit.deleted_expense_card(entry, trips.name_of(roster, member.id))])
    session.commit()


@router.post("/trips/{trip_id}/expenses/bulk", status_code=201)
def add_expenses(trip_id: RowId, body: inputs.BulkExpenseInput, session: auth.Database, member: MemberUser,
                 background: BackgroundTasks) -> dict:
    trip = _trip(session, trip_id, member)
    _open(trip)
    settle_currency = trip.settle_currency
    quotes = [trips.quote_amount(trip, row.amount, row.currency) for row in body.expenses]
    _begin(session)
    trip = _trip(session, trip_id, member)
    _open(trip)
    if trip.settle_currency != settle_currency:
        raise HTTPException(409, "結算幣別已有變動，請重新整理帳本")
    roster = trips.people(session, trip_id)
    entries = []
    for row, quoted in zip(body.expenses, quotes, strict=True):
        shares = _build_shares(row, roster)
        entry = trips.record_expense(session, trip_id, item=row.item, amount=row.amount, currency=row.currency,
                                     quote=quoted, spent_on=row.spent_on, payer_id=row.payer_id, creator_id=member.id,
                                     shares=shares, source_text=body.text, source=body.source,
                                     merchant=row.merchant or None, kind=row.kind)
        if row.category != entry.category and row.category != "待分類":
            trips.set_category(session, entry, row.category)
        entries.append(entry)
    ids = [entry.id for entry in entries]
    _notify_expenses(session, background, trip, entries, roster, "已記")
    session.commit()
    return {"ids": ids}


@router.post("/trips/{trip_id}/ai/parse")
def ai_parse(trip_id: RowId, body: inputs.AiText, session: auth.Database, member: MemberUser) -> dict:
    trip = _trip(session, trip_id, member)
    try:
        return web_ai.drafts(session, trip, member, body.text)
    except CommandError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/trips/{trip_id}/ai/photo")
async def ai_photo(trip_id: RowId, request: Request, session: auth.Database, member: MemberUser) -> dict:
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in gemini.IMAGE_TYPES:
        raise HTTPException(415, "請上傳 JPEG、PNG、WebP 或 HEIC 照片")
    if int(request.headers.get("content-length") or 0) > gemini.MAX_IMAGE_BYTES:
        raise HTTPException(413, "照片需小於 10 MB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > gemini.MAX_IMAGE_BYTES:
            raise HTTPException(413, "照片需小於 10 MB")

    def parse() -> dict:
        trip = _trip(session, trip_id, member)
        try:
            return web_ai.drafts(session, trip, member, None, image=(bytes(data), mime))
        except CommandError as exc:
            raise HTTPException(422, str(exc)) from exc

    return await run_in_threadpool(parse)


@router.post("/trips/{trip_id}/transfers", status_code=201)
def add_transfer(trip_id: RowId, body: inputs.TransferInput, session: auth.Database, member: MemberUser,
                 background: BackgroundTasks) -> dict:
    trip = _trip(session, trip_id, member)
    suggested = body.settlement_version is not None
    if not suggested:
        _open(trip)
    settle_currency = trip.settle_currency
    rate, value = (1, float(body.amount)) if suggested else trips.quote_amount(trip, body.amount, body.currency)
    _begin(session)
    trip = _trip(session, trip_id, member)
    if suggested:
        if member.id not in (body.from_id, trip.created_by):
            raise HTTPException(403, "只有付款人或帳本建立者可以標記已付")
        if (body.settlement_version != trip.ledger_version or body.currency != trip.settle_currency
            or not trips.suggested_payment(session, trip, split.Payment(body.from_id, body.to_id, body.amount))):
            raise HTTPException(409, "結算已更新，請重新整理帳本")
    else:
        _open(trip)
    if trip.settle_currency != settle_currency:
        raise HTTPException(409, "結算幣別已有變動，請重新整理帳本")
    if body.from_id == body.to_id or any(not session.get(TripMember, (trip_id, key)) for key in (body.from_id, body.to_id)):
        raise HTTPException(422, "請選擇兩位不同的帳本成員")
    entry = Transfer(trip_id=trip_id, from_id=body.from_id, to_id=body.to_id, amount=float(body.amount),
                     currency=body.currency, rate=rate, amount_settle=value, created_by=member.id)
    session.add(entry)
    session.flush()
    key = entry.id
    if suggested:
        trips.audit(session, trip_id, member.id, "web", "標記已付", f"#{key} 成員 #{body.from_id} → #{body.to_id} {body.amount}")
    _notify_transfer(session, background, trip, entry, "已記還款")
    session.commit()
    return {"id": key}


def _notify_transfer(session: Session, background: BackgroundTasks, trip: Trip, entry: Transfer,
                     title: str, *, deleted_by: int | None = None) -> None:
    roster = trips.people(session, trip.id)
    payer, recipient = trips.name_of(roster, entry.from_id), trips.name_of(roster, entry.to_id)
    if deleted_by is not None:
        title = f"{trips.name_of(roster, deleted_by)} {title}"
    amount = money(entry.amount, entry.currency)
    pending = "（待換算）" if entry.amount_settle is None else ""
    _notify_group(session, background, trip, [line.card_message(f"{title}：{payer} → {recipient} {amount}{pending}", [])])


@router.delete("/trips/{trip_id}/transfers/{transfer_id}", status_code=204)
def delete_transfer(trip_id: RowId, transfer_id: RowId, session: auth.Database, member: MemberUser,
                    background: BackgroundTasks) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    entry = session.get(Transfer, transfer_id)
    if entry is None or entry.trip_id != trip_id or entry.deleted_at is not None:
        raise HTTPException(404, "找不到這筆還款")
    entry.deleted_at = utcnow()
    trips.audit(session, trip_id, member.id, "web", "刪除還款", f"#{entry.id} {entry.amount:g} {entry.currency}")
    _notify_transfer(session, background, trip, entry, "已刪除還款", deleted_by=member.id)
    session.commit()


@router.put("/trips/{trip_id}/members/{member_id}", status_code=204)
def edit_member(trip_id: RowId, member_id: RowId, body: inputs.MemberInput, session: auth.Database, member: MemberUser) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _open(trip)
    target = session.get(TripMember, (trip_id, member_id))
    if target is None:
        raise HTTPException(404, "找不到這位成員")
    if member_id != member.id or target.active != body.active:
        _require_organizer(trip, member)
    if trip.created_by == member_id and not body.active:
        raise HTTPException(422, "帳本建立者需保留為使用中的成員")
    if target.active != body.active:
        trips.audit(session, trip_id, member.id, "web", "恢復分攤" if body.active else "停用分攤", f"成員 #{member_id}")
    target.nickname, target.weight, target.active = body.nickname, float(body.weight), body.active
    session.commit()


@router.put("/trips/{trip_id}/members/{member_id}/payment-note", status_code=204)
def edit_payment_note(trip_id: RowId, member_id: RowId, body: inputs.PaymentNoteInput,
                      session: auth.Database, member: MemberUser) -> None:
    _begin(session)
    _trip(session, trip_id, member)
    if member_id != member.id:
        raise HTTPException(403, "只能修改自己的收款方式")
    membership = session.get(TripMember, (trip_id, member_id))
    membership.payment_note = body.payment_note
    session.commit()


@router.post("/trips/{trip_id}/members", status_code=201)
def add_manual_member(trip_id: RowId, body: inputs.ManualMember, session: auth.Database, member: MemberUser) -> dict:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _open(trip)
    _require_organizer(trip, member)
    target = Member(line_name=body.nickname)
    session.add(target)
    session.flush()
    key = target.id
    session.add(TripMember(trip_id=trip_id, member_id=key, nickname=body.nickname, weight=float(body.weight)))
    session.commit()
    return {"id": key}


@router.delete("/trips/{trip_id}/members/{member_id}", status_code=204)
def delete_member(trip_id: RowId, member_id: RowId, session: auth.Database, member: MemberUser) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _require_organizer(trip, member)
    target = session.get(TripMember, (trip_id, member_id))
    if target is None:
        raise HTTPException(404, "找不到這位成員")
    if trip.created_by == member_id:
        raise HTTPException(409, "帳本建立者不能刪除")

    # 已刪除的支出也可能復原，因此一併保留對應的成員。
    references = (
        select(Expense.id).where(Expense.trip_id == trip_id, Expense.payer_id == member_id),
        select(ExpenseShare.expense_id).join(Expense, Expense.id == ExpenseShare.expense_id)
        .where(Expense.trip_id == trip_id, ExpenseShare.member_id == member_id),
        select(Transfer.id).where(Transfer.trip_id == trip_id, or_(Transfer.from_id == member_id, Transfer.to_id == member_id)),
    )
    if any(session.exec(query.limit(1)).first() is not None for query in references):
        detail = "此成員已有付款、分攤或還款紀錄，需保留歷史帳目。"
        if trip.status == "open":
            detail += "可在編輯成員時取消「參與新支出的分攤」。"
        raise HTTPException(409, detail)

    if trip.default_payer_id == member_id:
        trip.default_payer_id = None
    session.delete(target)
    trips.audit(session, trip_id, member.id, "web", "刪除成員", f"成員 #{member_id}")
    session.commit()


@router.put("/trips/{trip_id}/settings", status_code=204)
def update_settings(trip_id: RowId, body: inputs.TripSettings, session: auth.Database, member: MemberUser) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _open(trip)
    _require_organizer(trip, member)
    if trip.settle_currency != body.settle_currency and _has_records(session, trip_id):
        raise HTTPException(409, "已有支出或還款，為保留歷史換算結果，結算幣別已鎖定")
    trip.sqlmodel_update(body.model_dump(exclude={"fixed_rates", "notify_web_changes", "default_payer_id"}))
    if body.notify_web_changes is not None and body.notify_web_changes != trip.notify_web_changes:
        trip.notify_web_changes = body.notify_web_changes
        trips.audit(session, trip_id, member.id, "web", "開啟群組通知" if body.notify_web_changes else "關閉群組通知")
    if body.default_payer_id is not None and (body.default_payer_id or None) != trip.default_payer_id:
        # 0 表示記帳的人；沒帶欄位的舊版網頁不會改到這個設定。
        roster = trips.people(session, trip_id)
        if body.default_payer_id and (body.default_payer_id not in roster or not roster[body.default_payer_id][0].active):
            raise HTTPException(422, "預設付款人必須是參與分攤的帳本成員")
        trip.default_payer_id = body.default_payer_id or None
        label = trips.name_of(roster, trip.default_payer_id) if trip.default_payer_id else "記帳的人"
        trips.audit(session, trip_id, member.id, "web", "設定預設付款人", label)
    trip.fixed_rates = {key: float(value) for key, value in body.fixed_rates.items()}
    session.commit()


@router.post("/trips/{trip_id}/close", status_code=204)
def close_trip(trip_id: RowId, session: auth.Database, member: MemberUser, background: BackgroundTasks) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _require_organizer(trip, member)
    if trip.status != "closed":
        trips.audit(session, trip_id, member.id, "web", "結束帳本")
        trip.status = "closed"
        # 和 LINE「結束帳本」一樣把結算結果送到群組；重複按結束不會再推播。
        _notify_group(session, background, trip, line_chat.closing_summary(session, trip, member.id))
    session.commit()


@router.post("/trips/{trip_id}/reopen", status_code=204)
def reopen_trip(trip_id: RowId, session: auth.Database, member: MemberUser) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _require_organizer(trip, member)
    if trip.status == "open":
        return
    if trip.line_group_id and trips.open_in_group(session, trip.line_group_id):
        raise HTTPException(409, "這個 LINE 群組已有進行中的帳本，請先結束那本帳本再重新開啟。")
    trip.status = "open"
    trips.audit(session, trip_id, member.id, "web", "重新開啟帳本")
    session.commit()


@router.delete("/trips/{trip_id}", status_code=204)
def delete_trip(trip_id: RowId, session: auth.Database, member: MemberUser) -> None:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _require_organizer(trip, member)
    if trip.status != "closed":
        raise HTTPException(409, "請先結束帳本再刪除")
    trip.deleted_at = utcnow()
    trips.audit(session, trip_id, member.id, "web", "刪除帳本", trip.name)
    session.commit()


def _csv_text(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


@router.get("/trips/{trip_id}/export.csv")
def export_trip(trip_id: RowId, session: auth.Database, member: MemberUser) -> Response:
    trip = _trip(session, trip_id, member)
    roster = trips.people(session, trip_id)
    shares = trips.shares_by_expense(session, trip_id)
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["類型", "日期", "品項", "商家", "類別", "付款人", "分攤對象或收款人", "原幣", "金額", "結算幣別", "結算金額"])
    for row in trips.expenses(session, trip_id):
        people = "、".join(trips.name_of(roster, share.member_id) for share in shares.get(row.id, ()))
        refund = row.kind == "refund"
        writer.writerow(["退款" if refund else "支出", row.spent_on, _csv_text(row.item), _csv_text(row.merchant or ""),
                         _csv_text(row.category), _csv_text(trips.name_of(roster, row.payer_id)), _csv_text(people),
                         row.currency, -row.amount if refund else row.amount, trip.settle_currency,
                         -row.amount_settle if refund and row.amount_settle is not None else row.amount_settle])
    timezone = ZoneInfo(trip.timezone)
    for row in trips.transfers(session, trip_id):
        writer.writerow(["還款", row.created_at.astimezone(timezone).date(), "", "", "",
                         _csv_text(trips.name_of(roster, row.from_id)), _csv_text(trips.name_of(roster, row.to_id)),
                         row.currency, row.amount, trip.settle_currency, row.amount_settle])
    filename = quote(f"{trip.name}-帳本.csv", safe="")
    return Response(stream.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=trip-{trip_id}.csv; filename*=UTF-8''{filename}"})


@router.post("/admin/login")
def admin_login(body: inputs.AdminLogin) -> dict:
    return {"token": auth.admin_login(body.password)}


@router.get("/admin/overview")
def admin_overview(session: auth.Database, _: AdminUser) -> dict:
    return admin_report.overview(session)


@router.get("/admin/trips/{trip_id}")
def admin_trip(trip_id: RowId, session: auth.Database, _: AdminUser) -> dict:
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(404, "找不到這本帳")
    return admin_report.trip_detail(session, trip)


@router.post("/admin/trips/{trip_id}/restore", status_code=204)
def admin_restore_trip(trip_id: RowId, session: auth.Database, _: AdminUser) -> None:
    _begin(session)
    trip = session.get(Trip, trip_id)
    if trip is None or trip.deleted_at is None:
        raise HTTPException(404, "找不到已刪除的帳本")
    trip.deleted_at = None
    trips.audit(session, trip_id, None, "admin", "還原帳本", trip.name)
    session.commit()


@router.post("/admin/inbox/{row_id}/review", status_code=204)
def review_inbox(row_id: RowId, session: auth.Database, _: AdminUser) -> None:
    _begin(session)
    row = session.get(LineInbox, row_id)
    if row is None or row.status not in ("failed", "reviewed"):
        raise HTTPException(404, "找不到待核對的事件")
    row.status, row.payload = "reviewed", None
    session.commit()
