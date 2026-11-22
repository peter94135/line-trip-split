from fastapi import APIRouter, HTTPException

from app import auth, web_inputs as inputs
from app.models import LineInbox, Trip
from app.routers.web_common import AdminUser, RowId, _begin
from app.services import admin_report, trips

router = APIRouter()


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
