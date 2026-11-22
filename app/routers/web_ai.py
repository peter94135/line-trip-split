import math
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete
from sqlmodel import Session, select

from app import auth, web_inputs as inputs
from app.config import get_settings
from app.models import Member, Trip, WebAiRequest, utcnow
from app.routers.web_common import MemberUser, RowId, _begin, _open, _trip
from app.services import gemini, web_ai
from app.services.line_parse import CommandError

router = APIRouter()


def _reserve_ai(session: Session, trip_id: int, member: Member) -> Trip:
    _begin(session)
    trip = _trip(session, trip_id, member)
    _open(trip)
    now = utcnow()
    settings = get_settings()
    session.execute(delete(WebAiRequest).where(WebAiRequest.at <= now - timedelta(days=1)))
    times = session.exec(select(WebAiRequest.at).where(WebAiRequest.member_id == member.id)
                         .order_by(WebAiRequest.at.desc())).all()
    for seconds, limit, label in ((60, settings.web_ai_per_minute, "每分鐘"),
                                  (86400, settings.web_ai_per_day, "每 24 小時")):
        if len(times) >= limit:
            remaining = seconds - (now - times[limit - 1]).total_seconds()
            if remaining > 0:
                raise HTTPException(429, f"AI 記帳{label}最多 {limit} 次，請稍後再試。",
                                    headers={"Retry-After": str(math.ceil(remaining))})
    session.add(WebAiRequest(member_id=member.id, at=now))
    session.commit()  # 呼叫 Gemini 前先保留額度並釋放寫入鎖。
    return trip


@router.post("/trips/{trip_id}/ai/parse")
def ai_parse(trip_id: RowId, body: inputs.AiText, session: auth.Database, member: MemberUser) -> dict:
    trip = _reserve_ai(session, trip_id, member)
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
        trip = _reserve_ai(session, trip_id, member)
        try:
            return web_ai.drafts(session, trip, member, None, image=(bytes(data), mime))
        except CommandError as exc:
            raise HTTPException(422, str(exc)) from exc

    return await run_in_threadpool(parse)
