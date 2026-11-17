"""驗證候選支出並保存待確認的分攤快照。"""

import re
import secrets
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlmodel import Session

from app.models import LineChat, Share, Trip, utcnow
from app.services import gemini, line, split, trips
from app.services.line_parse import CommandError, Draft, money, normalize

DRAFT_TTL = timedelta(minutes=15)
MAX_EXPENSES = 5
CONFIRM = {"好": "confirm", "取消": "cancel"}
PHOTO_WAIT = timedelta(minutes=2)
RECENT_PHOTO = timedelta(minutes=5)


class ExpenseDraft(BaseModel):
    item: str
    kind: Literal["expense", "refund"] = "expense"
    amount: Decimal
    currency: str
    spent_on: date
    payer_id: int
    shares: list[Share]
    merchant: str | None = None
    category: str | None = None


class Pending(BaseModel):
    trip_id: int
    source: Literal["line_text", "gemini", "line_photo"]
    text: str | None
    expenses: list[ExpenseDraft]


def correction(text: str) -> str | None:
    return text[2:].lstrip(" :\t\n").strip() if text.startswith("改成") else None


def _amount(value: float | None) -> Decimal:
    if value is None:
        raise CommandError("這筆金額還不清楚。請補上實付金額後重新送出。")
    amount = Decimal(str(value))
    if not amount.is_finite() or not 0 < amount <= 999_999_999_999 or amount != amount.quantize(Decimal("0.01")):
        raise CommandError("請確認金額大於零、小於一兆，且最多兩位小數。")
    return amount


def from_gemini(parsed: gemini.TripParse, *, photo: bool, mention_tokens: dict[str, str]) -> list[Draft]:
    if parsed.clarification and parsed.clarification.strip():
        raise CommandError("需要確認：" + parsed.clarification.strip()[:300] + "\n請補充資訊後重新送出記帳指令。")
    if parsed.task_type != "expense_add":
        raise CommandError("請提供要記的支出，例如「記 晚餐 3000」。結算或還款可用「結算」、「已還 小明 1200」。")
    if not parsed.expenses:
        raise CommandError("找不到可記帳的品項與金額，請再補充資訊。")
    result = []
    for entry in parsed.expenses:
        item = normalize(entry.item)
        if not item or len(item) > 100:
            raise CommandError("請提供簡短的支出品項。")
        currency = normalize(entry.currency).upper() if entry.currency else None
        if (photo or currency) and not (currency and re.fullmatch(r"[A-Z]{3}", currency)):
            raise CommandError("請補上幣別，例如 TWD、JPY 或 USD。")
        spent_on = None
        if entry.spent_on:
            try:
                spent_on = date.fromisoformat(entry.spent_on)
                if spent_on.isoformat() != entry.spent_on:
                    raise ValueError
            except ValueError as exc:
                raise CommandError("請用 YYYY-MM-DD 提供支出日期。") from exc

        def name(value: str) -> str:
            return mention_tokens.get(normalize(value).lstrip("@"), normalize(value))

        result.append(Draft(
            item=item, amount=_amount(entry.amount), currency=currency, days_ago=0, kind=entry.kind,
            payer=name(entry.payer) if entry.payer else None, include=[name(value) for value in entry.split_among] or None,
            exclude=[name(value) for value in entry.exclude],
            exact=[(name(share.member), _amount(share.amount)) for share in entry.exact_shares], treat=entry.treat,
            merchant=(normalize(entry.merchant)[:100] or None) if entry.merchant else None,
            category=entry.category if entry.category in trips.CATEGORIES else None, spent_on=spent_on,
        ))
    return result


def prepare(drafts: list[Draft], trip: Trip, today: date, roster: dict, speaker: int,
            mentions: dict[str, int]) -> list[ExpenseDraft]:
    if len(drafts) > MAX_EXPENSES:
        raise CommandError("一次最多記五筆，請分開傳送。")
    result = []
    for draft in drafts:
        def resolve(name: str) -> int:
            return trips.resolve(roster, speaker, name, mentions)

        # 「我請客」一定是記帳的人付；其他沒講付款人的照帳本的預設付款人。
        payer_id = resolve(draft.payer) if draft.payer else speaker if draft.treat else trips.default_payer(trip, roster, speaker)
        include = [resolve(name) for name in draft.include] if draft.include else None
        exclude = [resolve(name) for name in draft.exclude]
        if draft.include and any(name in mentions for name in draft.include) and speaker not in exclude:
            include.append(speaker)
        exact = {resolve(name): amount for name, amount in draft.exact} if draft.exact else None
        if exact is not None and len(exact) != len(draft.exact):
            raise CommandError("指定金額的分攤成員重複。")
        try:
            shares = split.build_shares(draft.amount, payer_id, (row[0] for row in roster.values()), include=include,
                                       exclude=exclude, exact=exact, treat=draft.treat)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        result.append(ExpenseDraft(item=draft.item, kind=draft.kind, amount=draft.amount, currency=draft.currency or trip.default_currency,
                                   spent_on=draft.spent_on or today - timedelta(days=draft.days_ago), payer_id=payer_id,
                                   shares=shares, merchant=draft.merchant, category=draft.category))
    return result


def get(chat: LineChat | None) -> Pending | None:
    if (chat and chat.draft and chat.draft.get("kind") == "expenses" and chat.draft_nonce
            and chat.draft_at and utcnow() - chat.draft_at <= DRAFT_TTL):
        return Pending.model_validate(chat.draft)
    return None


def clear(chat: LineChat) -> None:
    chat.draft, chat.draft_nonce, chat.draft_at = None, None, None


def card(session: Session, chat: LineChat, pending: Pending, trip: Trip, roster: dict) -> list[dict]:
    nonce = secrets.token_urlsafe(8)
    chat.draft = {"kind": "expenses", **pending.model_dump(mode="json")}
    chat.draft_nonce, chat.draft_at = nonce, utcnow()
    lines = [f"待確認｜{trip.name}｜{len(pending.expenses)} 筆"]
    for entry in pending.expenses:
        shares = []
        for share in entry.shares:
            name = trips.name_of(roster, share.member_id)
            if share.amount is not None:
                name += f" {money(share.amount, entry.currency)}"
            elif share.weight != 1:
                name += f" ×{share.weight:g}"
            shares.append(name)
        category, _ = trips.resolve_category(session, entry.item, entry.merchant, entry.category)
        kind_label, verb = ("退款｜", "收") if entry.kind == "refund" else ("", "付")
        lines.append(f"{entry.spent_on.isoformat()} {kind_label}{entry.item} {money(entry.amount, entry.currency)} ({entry.currency})｜{category}")
        lines.append(f"{trips.name_of(roster, entry.payer_id)}{verb}｜分攤：{'、'.join(shares)}")
    # 「改成」只打開鍵盤並帶入文字，送出的文字訊息才會修改草稿。
    return [line.text_message("\n".join(lines), [("好", f"draft=confirm&nonce={nonce}"),
                                                 ("取消", f"draft=cancel&nonce={nonce}"),
                                                 ("改成", f"draft=edit&nonce={nonce}", "改成 ")])]


def previous_entries(pending: Pending, roster: dict) -> list[dict]:
    result = []
    for entry in pending.expenses:
        shares = [dict(member=trips.name_of(roster, share.member_id), amount=share.amount) for share in entry.shares]
        exact = shares[0]["amount"] is not None
        result.append(dict(item=entry.item, kind=entry.kind, merchant=entry.merchant, amount=str(entry.amount), currency=entry.currency,
                           spent_on=entry.spent_on.isoformat(), category=entry.category,
                           payer=trips.name_of(roster, entry.payer_id),
                           split_among=[] if exact else [share["member"] for share in shares],
                           exact_shares=shares if exact else []))
    return result
