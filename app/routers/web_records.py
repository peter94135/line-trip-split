from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlmodel import Session

from app import auth, web_inputs as inputs
from app.models import Expense, Member, Transfer, Trip, TripMember, utcnow
from app.routers.web_common import MemberUser, RowId, _begin, _notify_group, _open, _trip
from app.services import line, line_edit, split, trips
from app.services.line_parse import money

router = APIRouter()


def _expense(session: Session, trip: Trip, expense_id: int) -> Expense:
    entry = session.get(Expense, expense_id)
    if entry is None or entry.trip_id != trip.id or entry.deleted_at is not None:
        raise HTTPException(404, "找不到這筆支出")
    return entry

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
    if "category" in body.model_fields_set and body.category != entry.category and body.category != "待分類":
        trips.set_category(session, entry, body.category)
    elif "category" in body.model_fields_set:
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
                     currency=body.currency, rate=rate, amount_settle=value, created_by=member.id, source="web")
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
