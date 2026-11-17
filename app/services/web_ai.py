"""網頁 AI 記帳：沿用 LINE 的解析與分攤驗證，只產生草稿，確認後才由批次 API 入帳。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.config import get_settings
from app.models import Member, Trip, utcnow
from app.services import gemini, line_draft, trips
from app.services.line_parse import CommandError, Draft, normalize, parse_quick

PHOTO_PROMPT = "讀取這張收據"


def _gemini(session: Session, trip: Trip, member: Member, text: str, today, image=None) -> tuple[list[Draft], str]:
    roster = trips.people(session, trip.id)
    members = [dict(nickname=membership.nickname, line_name=person.line_name, active=membership.active,
                    speaker=person.id == member.id) for membership, person in roster.values()]
    try:
        parsed = gemini.parse(text, today=today, currency=trip.default_currency, members=members, mentions={}, image=image)
    except gemini.GeminiError as exc:
        raise CommandError(str(exc)) from exc
    source = "web_photo" if image else "web_ai"
    return line_draft.from_gemini(parsed, photo=image is not None, mention_tokens={}), source


def drafts(session: Session, trip: Trip, member: Member, text: str | None,
           image: tuple[bytes, str] | None = None, when: datetime | None = None) -> dict:
    try:
        return _drafts(session, trip, member, text, image, when)
    except CommandError as exc:
        # LINE 的提示都以「記」開頭；網頁輸入框不需要。
        raise CommandError(str(exc).replace("「記 ", "「").replace("「記」", "「」")) from exc


def _drafts(session: Session, trip: Trip, member: Member, text: str | None,
            image: tuple[bytes, str] | None, when: datetime | None) -> dict:
    if trip.status != "open":
        raise CommandError("帳本已結束，無法記帳。")
    today = (when or utcnow()).astimezone(ZoneInfo(trip.timezone)).date()
    text = normalize(text or "")
    configured = bool(get_settings().gemini_api_key)
    if image is not None:
        if not configured:
            raise CommandError("目前沒有設定 AI，請改用文字或手動記帳。")
        parsed, source = _gemini(session, trip, member, text or PHOTO_PROMPT, today, image)
    else:
        if not text:
            raise CommandError("請輸入要記的支出，例如「晚餐 3000」。")
        try:
            parsed, source = parse_quick(text), "web"
        except CommandError:
            if not configured:
                raise
            parsed, source = _gemini(session, trip, member, text, today)
    roster = trips.people(session, trip.id)
    entries = line_draft.prepare(parsed, trip, today, roster, member.id, {})
    result = []
    for entry in entries:
        exact = any(share.amount is not None for share in entry.shares)
        suggested = entry.category or (gemini.guess_category(session, entry.item, entry.merchant) if source == "web" else None)
        category, _ = trips.resolve_category(session, entry.item, entry.merchant, suggested)
        result.append(dict(
            item=entry.item, kind=entry.kind, merchant=entry.merchant, spent_on=entry.spent_on, amount=entry.amount,
            currency=entry.currency, payer_id=entry.payer_id, category=category, split_mode="exact" if exact else "equal",
            shares=[{"member_id": share.member_id, **({"amount": share.amount} if exact else {"weight": share.weight})}
                    for share in entry.shares],
        ))
    return {"source": source, "text": text or None, "expenses": result}
