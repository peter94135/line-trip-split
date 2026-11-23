from fastapi import APIRouter, HTTPException

from app import auth, web_inputs as inputs
from app.models import AdminPushSubscription, LineInbox, Trip
from app.routers.web_common import AdminUser, RowId, _begin
from app.services import admin_push, admin_report, trips

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


@router.get("/admin/push")
def push_status(session: auth.Database, _: AdminUser) -> dict:
    return {"public_key": admin_push.public_key(), "devices": admin_push.subscription_count(session)}


@router.post("/admin/push/subscribe", status_code=204)
def push_subscribe(body: inputs.PushSubscription, session: auth.Database, _: AdminUser) -> None:
    session.merge(AdminPushSubscription(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth))
    session.commit()


@router.post("/admin/push/unsubscribe", status_code=204)
def push_unsubscribe(body: inputs.PushEndpoint, session: auth.Database, _: AdminUser) -> None:
    if row := session.get(AdminPushSubscription, body.endpoint):
        session.delete(row)
        session.commit()


@router.post("/admin/push/test")
def push_test(_: AdminUser) -> dict:
    sent = admin_push.send("測試通知", "管理頁通知已開啟。服務有異常時會通知這台裝置。", tag="test")
    if not sent:
        raise HTTPException(409, "沒有可送達的裝置，請先開啟通知")
    return {"sent": sent}
