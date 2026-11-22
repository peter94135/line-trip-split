from collections.abc import Iterable
from typing import Annotated

from fastapi import BackgroundTasks, Depends, HTTPException, Path
from sqlmodel import Session

from app import auth
from app.config import get_settings
from app.models import Member, Trip, TripMember
from app.services import line, line_delivery

MemberUser = Annotated[Member, Depends(auth.require_member)]
AdminUser = Annotated[None, Depends(auth.require_admin)]
RowId = Annotated[int, Path(gt=0, le=9_223_372_036_854_775_807)]


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

def _notify_group(session: Session, background: BackgroundTasks, trip: Trip, messages: Iterable[dict]) -> None:
    if not trip.line_group_id or not trip.notify_web_changes or not line.is_configured():
        return
    # 通知和帳目一起存檔，送出在回覆 HTTP 後，服務重啟時也能補送。
    delivery_id = line_delivery.enqueue(session, trip.line_group_id, list(messages))
    background.add_task(line_delivery.send, delivery_id)
