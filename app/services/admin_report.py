"""管理頁的唯讀報表：帳本概況、單本帳本詳細資料、LINE 與系統狀態。"""

import json
import platform
import threading
import time
from dataclasses import asdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    AuditLog, Expense, ExpenseShare, GeminiCall, LineDelivery, LineInbox, Member, Setting, Transfer, Trip, TripMember,
    utcnow,
)
from app.services import admin_push, fx, gemini, line, trip_stats, trips

STARTED_AT = utcnow()
QUOTA_TTL_S = 600
_quota_lock = threading.Lock()
_quota_cache: tuple[float, dict | None] = (0.0, None)


def _names(session: Session) -> dict[int, str]:
    return {row.id: row.line_name or f"成員 #{row.id}" for row in session.exec(select(Member))}


def _latest(session: Session, model, column) -> dict[int, object]:
    return dict(session.exec(select(model.trip_id, func.max(column)).group_by(model.trip_id)).all())


def trip_rows(session: Session) -> list[dict]:
    names = _names(session)
    members = dict(session.exec(select(TripMember.trip_id, func.count()).group_by(TripMember.trip_id)).all())
    live = {(trip_id, kind): count for trip_id, kind, count in session.exec(
        select(Expense.trip_id, Expense.kind, func.count()).where(Expense.deleted_at.is_(None))
        .group_by(Expense.trip_id, Expense.kind))}
    removed = dict(session.exec(select(Expense.trip_id, func.count()).where(Expense.deleted_at.is_not(None))
                                .group_by(Expense.trip_id)).all())
    activity = [_latest(session, Expense, Expense.created_at), _latest(session, Transfer, Transfer.created_at),
                _latest(session, AuditLog, AuditLog.at)]
    rows = []
    for trip in session.exec(select(Trip).order_by(Trip.id.desc())):
        moments = [value for source in activity if (value := source.get(trip.id))] + [trip.created_at]
        rows.append(dict(
            id=trip.id, name=trip.name, status=trip.status, start_date=trip.start_date, end_date=trip.end_date,
            settle_currency=trip.settle_currency, creator=names.get(trip.created_by, "—"),
            line_group=bool(trip.line_group_id), members=members.get(trip.id, 0),
            expenses=live.get((trip.id, "expense"), 0), refunds=live.get((trip.id, "refund"), 0),
            deleted_expenses=removed.get(trip.id, 0), total=trips.total(session, trip).total,
            last_activity=max(moments), created_at=trip.created_at, deleted_at=trip.deleted_at,
        ))
    return rows


def line_quota() -> dict | None:
    """本月推播額度。LINE 的查詢有次數限制，所以快取十分鐘；查不到就回 None。"""
    global _quota_cache
    if not line.is_configured():
        return None
    with _quota_lock:
        fetched, cached = _quota_cache
        if time.monotonic() - fetched < QUOTA_TTL_S:
            return cached
        try:
            quota = line._request("GET", "/message/quota")
            usage = line._request("GET", "/message/quota/consumption")
            cached = {"type": quota.get("type"), "limit": quota.get("value"), "used": usage.get("totalUsage")}
        except line.LineError:
            cached = None
        _quota_cache = (time.monotonic(), cached)
        return cached


def _line_status(session: Session) -> dict:
    since = utcnow() - timedelta(hours=24)
    recent = dict(session.exec(select(LineInbox.status, func.count()).where(LineInbox.received_at >= since)
                               .group_by(LineInbox.status)).all())
    failed = session.exec(select(LineInbox).where(LineInbox.status == "failed").order_by(LineInbox.id.desc()).limit(50)).all()
    return {
        "recent": recent,
        "failed": [{"id": row.id, "type": (row.payload or {}).get("type", "未知"), "received_at": row.received_at}
                   for row in failed],
        "delivery": dict(session.exec(select(LineDelivery.status, func.count()).group_by(LineDelivery.status)).all()),
        "quota": line_quota(),
    }


def _gemini_status(session: Session) -> dict:
    now = utcnow()
    usage = session.exec(select(GeminiCall.model, func.count(), func.sum(GeminiCall.input_tokens),
                                func.sum(GeminiCall.output_tokens), func.sum(GeminiCall.cost_usd))
                         .where(GeminiCall.at >= now - timedelta(days=30)).group_by(GeminiCall.model)
                         .order_by(GeminiCall.model)).all()
    timezone = ZoneInfo(get_settings().tz)
    daily: dict[str, int] = {}
    for at in session.exec(select(GeminiCall.at).where(GeminiCall.at >= now - timedelta(days=7))).all():
        day = at.astimezone(timezone).date().isoformat()
        daily[day] = daily.get(day, 0) + 1
    models = []
    for model in gemini._models():
        row = session.get(Setting, f"gemini_model:{model}")
        retry_at = json.loads(row.value).get("retry_at", 0) if row else 0
        models.append({"model": model, "cooling_until": retry_at if retry_at > time.time() else None})
    return {
        "configured": bool(get_settings().gemini_api_key),
        "usage": [dict(model=model, calls=calls, input_tokens=inputs, output_tokens=outputs, cost_usd=cost)
                  for model, calls, inputs, outputs, cost in usage],
        "daily": [{"day": day, "calls": daily[day]} for day in sorted(daily)],
        "models": models,
    }


def _system(session: Session) -> dict:
    settings = get_settings()
    sizes = {suffix or "db": (settings.db_path.with_name(settings.db_path.name + suffix)) for suffix in ("", "-wal")}
    row = session.get(Setting, fx.SETTING)
    cached = json.loads(row.value) if row else {}
    return {
        "version": settings.app_version or "未標記",
        "started_at": STARTED_AT,
        "python": platform.python_version(),
        "db_bytes": sum(path.stat().st_size for path in sizes.values() if path.exists()),
        "fx_date": cached.get("date"),
        "fx_source": fx.ATTRIBUTION,
        "audit_count": session.exec(select(func.count()).select_from(AuditLog)).one(),
    }


def overview(session: Session) -> dict:
    return {"trips": trip_rows(session), "line": _line_status(session), "gemini": _gemini_status(session),
            "system": _system(session), "alerts": admin_push.active_alerts(session),
            "push_devices": admin_push.subscription_count(session)}


def trip_detail(session: Session, trip: Trip) -> dict:
    names = _names(session)
    roster = trips.people(session, trip.id)
    expenses = session.exec(select(Expense).where(Expense.trip_id == trip.id)
                            .order_by(Expense.spent_on.desc(), Expense.id.desc())).all()
    share_counts = dict(session.exec(select(ExpenseShare.expense_id, func.count())
                                     .join(Expense, Expense.id == ExpenseShare.expense_id)
                                     .where(Expense.trip_id == trip.id).group_by(ExpenseShare.expense_id)).all())
    transfers = session.exec(select(Transfer).where(Transfer.trip_id == trip.id)
                             .order_by(Transfer.created_at.desc(), Transfer.id.desc())).all()
    logs = session.exec(select(AuditLog).where(AuditLog.trip_id == trip.id).order_by(AuditLog.id.desc()).limit(200)).all()
    settlement = trips.total(session, trip)
    who = lambda member_id: trips.name_of(roster, member_id) if member_id in roster else names.get(member_id, "—")
    return {
        "trip": dict(id=trip.id, name=trip.name, status=trip.status, start_date=trip.start_date, end_date=trip.end_date,
                     timezone=trip.timezone, default_currency=trip.default_currency, settle_currency=trip.settle_currency,
                     fixed_rates=trip.fixed_rates or {}, creator=who(trip.created_by), line_group=bool(trip.line_group_id),
                     created_at=trip.created_at, deleted_at=trip.deleted_at),
        "members": [dict(id=key, name=trips.name_of(roster, key), line_name=person.line_name, nickname=membership.nickname,
                         weight=membership.weight, active=membership.active, joined_at=membership.joined_at,
                         paid=settlement.paid.get(key, 0), owed=settlement.owed.get(key, 0),
                         balance=settlement.balances.get(key, 0))
                    for key, (membership, person) in roster.items()],
        "expenses": [dict(id=row.id, spent_on=row.spent_on, item=row.item, kind=row.kind, merchant=row.merchant,
                          category=row.category, amount=row.amount, currency=row.currency, amount_settle=row.amount_settle,
                          payer=who(row.payer_id), shares=share_counts.get(row.id, 0), source=row.source,
                          created_by=who(row.created_by), created_at=row.created_at,
                          deleted_at=row.deleted_at, deleted_by=who(row.deleted_by) if row.deleted_by else None)
                     for row in expenses],
        "transfers": [dict(id=row.id, sender=who(row.from_id), recipient=who(row.to_id), amount=row.amount,
                           currency=row.currency, created_by=who(row.created_by), created_at=row.created_at,
                           deleted_at=row.deleted_at) for row in transfers],
        "settlement": {**{key: value for key, value in asdict(settlement).items() if key not in ("shares", "transfers")},
                       "payments": [dict(sender=who(row.from_id), recipient=who(row.to_id),
                                                               amount=row.amount) for row in settlement.payments]},
        "stats": trip_stats.calculate(session, trip),
        "audit": [dict(at=row.at, actor=who(row.actor_id) if row.actor_id else "管理員", channel=row.channel,
                       action=row.action, detail=row.detail) for row in logs],
    }
