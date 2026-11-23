"""每分鐘檢查需要架設者處理的異常，交給 admin_push 通知開始與恢復。"""

import time
from datetime import timedelta

from sqlmodel import Session, func, select

from app.config import get_settings
from app.db import get_engine
from app.models import Expense, LineDelivery, LineInbox, Transfer, Trip, utcnow
from app.services import admin_push, admin_report, gemini, line

STUCK_DELIVERY = timedelta(minutes=10)
MISSING_RATE = timedelta(days=1)
QUOTA_LEVELS = (80, 100)


def _count(session: Session, query) -> int:
    return session.exec(select(func.count()).select_from(query.subquery())).one()


def _missing_rates(session: Session) -> int:
    since = utcnow() - MISSING_RATE
    total = 0
    for model in (Expense, Transfer):
        total += _count(session, select(model.id).join(Trip, model.trip_id == Trip.id).where(
            model.amount_settle.is_(None), model.deleted_at.is_(None), Trip.deleted_at.is_(None),
            model.created_at < since))
    return total


def _quota_percent() -> float | None:
    quota = admin_report.line_quota()
    if not quota or quota.get("type") != "limited" or not quota.get("limit") or quota.get("used") is None:
        return None
    return quota["used"] * 100 / quota["limit"]


def _gemini_down() -> bool:
    if not get_settings().gemini_api_key:
        return False
    with Session(get_engine()) as session:
        return all(gemini._stat(session, model).get("retry_at", 0) > time.time() for model in gemini._models())


def check() -> None:
    now = utcnow()
    with Session(get_engine()) as session:
        failed = _count(session, select(LineInbox.id).where(LineInbox.status == "failed"))
        stuck = _count(session, select(LineDelivery.id).where(
            (LineDelivery.status == "pending") & (LineDelivery.created_at < now - STUCK_DELIVERY)
            | (LineDelivery.status == "failed") & (LineDelivery.created_at >= now - timedelta(days=1))))
        missing = _missing_rates(session)

    admin_push.condition("line_failed", failed, "LINE 事件待核對", f"有 {failed} 則 LINE 事件處理失敗，請到管理頁核對。",
                         recovered="待核對的 LINE 事件都已處理。", url="/admin#line-failed")
    admin_push.condition("line_delivery", stuck, "LINE 回覆送不出去",
                         f"有 {stuck} 筆回覆或推播卡住或送出失敗，群組可能沒收到通知。",
                         recovered="近 24 小時沒有卡住或失敗的 LINE 回覆。", url="/admin#line-status")
    admin_push.condition("fx_missing", missing, "匯率一直抓不到",
                         f"有 {missing} 筆外幣紀錄超過一天仍待換算，結算暫未計入。",
                         recovered="外幣紀錄都已換算。", url="/admin#system")
    down = _gemini_down()
    admin_push.condition("gemini_down", int(down), "Gemini 暫時無法使用",
                         "主要與備援模型都在冷卻中，AI 記帳與收據辨識暫停。", recovered="Gemini 模型已可使用。",
                         url="/admin#gemini")
    if line.is_configured() and (percent := _quota_percent()) is not None:
        for level in QUOTA_LEVELS:
            reached = int(percent >= level)
            body = (f"本月推播額度已用 {percent:.0f}%。" + ("網頁改帳的群組通知會送不出去，可在帳本設定關閉通知。"
                                                     if level == 100 else "人多的群組可考慮關閉網頁改帳通知。"))
            admin_push.condition(f"quota_{level}", reached, f"LINE 推播額度用到 {level}%", body,
                                 recovered="LINE 推播額度已重置。", url="/admin#line-status")
