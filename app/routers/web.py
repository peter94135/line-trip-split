"""LIFF 與管理頁的 API。帳本資料沿用 LINE 的 SQLite 資料與分攤服務。"""

import csv
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response
from sqlalchemy import union
from sqlmodel import Session, select

from app import auth, web_inputs as inputs
from app.config import get_settings
from app.models import Expense, ExpenseShare, Member, Transfer, Trip, TripMember, utcnow
from app.routers import web_admin, web_ai, web_records
from app.routers.web_common import MemberUser, RowId, _begin, _notify_group, _open, _organizer, _require_organizer, _trip
from app.services import fx, line, line_settlement, split, trip_stats, trips
from app.services.line_parse import CURRENCIES

router = APIRouter(prefix="/api")
router.include_router(web_records.router)
router.include_router(web_ai.router)
router.include_router(web_admin.router)

_TRIP_FIELDS = {"id", "name", "status", "start_date", "end_date", "timezone", "default_currency", "settle_currency"}
_EXPENSE_FIELDS = {"id", "spent_on", "item", "kind", "merchant", "category", "amount", "currency", "amount_settle", "payer_id", "split_mode", "created_by", "created_at", "source"}
_TRANSFER_FIELDS = {"id", "from_id", "to_id", "amount", "currency", "amount_settle", "created_at", "created_by", "source"}


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


def _record_members(session: Session, trip_id: int) -> set[int]:
    # 包含可復原的紀錄。
    queries = [select(column).where(model.trip_id == trip_id) for model, column in
               ((Expense, Expense.payer_id), (Transfer, Transfer.from_id), (Transfer, Transfer.to_id))]
    queries.append(select(ExpenseShare.member_id).join(Expense, Expense.id == ExpenseShare.expense_id)
                   .where(Expense.trip_id == trip_id))
    return set(session.execute(union(*queries)).scalars())


@router.get("/trips/{trip_id}/category")
def suggest_category(trip_id: RowId, session: auth.Database, member: MemberUser,
                     item: Annotated[str, Query(max_length=100)], merchant: Annotated[str, Query(max_length=100)] = "") -> dict:
    _trip(session, trip_id, member)
    category, _ = trips.resolve_category(session, item, merchant, None)
    return {"category": category}


@router.get("/trips/{trip_id}")
def trip_detail(trip_id: RowId, session: auth.Database, member: MemberUser) -> dict:
    session.connection().exec_driver_sql("BEGIN")
    trip = _trip(session, trip_id, member)
    group_count = line.group_count(trip.line_group_id) if trip.status == "open" else None
    roster = trips.people(session, trip_id)
    referenced = _record_members(session, trip_id)
    shares = trips.shares_by_expense(session, trip_id)
    expenses = trips.expenses(session, trip_id)
    transfers = trips.transfers(session, trip_id)
    former_ids = {entry.created_by for entry in (*expenses, *transfers) if entry.created_by and entry.created_by not in roster}
    former_names = dict(session.exec(select(Member.id, Member.line_name).where(Member.id.in_(former_ids))).all()) if former_ids else {}
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
        "group_warning": trips.group_warning(roster, group_count),
        "members": [dict(id=key, name=trips.name_of(roster, key), nickname=membership.nickname,
                         can_delete=key != trip.created_by and key not in referenced,
                         weight=membership.weight, active=membership.active, organizer=key == trip.created_by,
                         payment_note=membership.payment_note)
                    for key, (membership, _) in roster.items()],
        "expenses": [dict(**row.model_dump(include=_EXPENSE_FIELDS), creator_name=former_names.get(row.created_by),
                          shares=[share.model_dump(exclude={"expense_id"}) for share in shares.get(row.id, ())])
                     for row in expenses],
        "transfers": [dict(**row.model_dump(include=_TRANSFER_FIELDS), creator_name=former_names.get(row.created_by)) for row in transfers],
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
    if member_id in _record_members(session, trip_id):
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
        actor = trips.name_of(trips.people(session, trip.id), member.id)
        _notify_group(session, background, trip, [line_settlement.card(session, trip, closing=f"{actor} 在網頁結束帳本")])
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
