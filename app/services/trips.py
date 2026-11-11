"""帳本成員、支出、還款與分類的共用資料操作。"""

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from app.db import get_engine
from app.models import AuditLog, CategoryMemory, Expense, ExpenseShare, Member, Share, Transfer, Trip, TripMember
from app.services import fx, split
from app.services.line_parse import CommandError, normalize

log = logging.getLogger(__name__)
KEYWORDS = {
    "伴手禮": "伴手禮 紀念品 送禮 名產 手信",
    "餐飲": "早餐 午餐 晚餐 宵夜 便當 飲料 咖啡 拉麵 火鍋 燒肉 壽司 餐廳 居酒屋 酒 啤酒 美食 小吃 食堂",
    "交通": "高鐵 台鐵 火車 捷運 公車 客運 計程車 uber 停車 機票 地鐵 新幹線 租車 suica 車票",
    "住宿": "飯店 旅館 民宿 住宿 airbnb agoda booking hotel",
    "門票娛樂": "電影 門票 演唱會 遊樂園 環球影城 迪士尼 展覽 溫泉",
    "購物": "衣服 鞋子 包包 uniqlo 百貨 藥妝 超商 全家 7-11 lawson 全聯",
    "雜支": "行李寄放 小費 網卡 esim 手續費",
}
CATEGORIES = ("餐飲", "交通", "住宿", "門票娛樂", "購物", "伴手禮", "雜支", "待分類")


def audit(session: Session, trip_id: int | None, actor_id: int | None, channel: str, action: str,
          detail: str = "") -> None:
    session.add(AuditLog(trip_id=trip_id, actor_id=actor_id, channel=channel, action=action, detail=detail[:300]))
    log.info("%s 成員 #%s %s：%s（帳本 #%s）", channel, actor_id, action, detail, trip_id)


def invite_url(trip: Trip, liff_id: str) -> str:
    code = quote(trip.invite_code, safe="")
    base = f"https://liff.line.me/{quote(liff_id, safe='')}" if liff_id else "/liff/"
    return f"{base}?join={code}"


def open_in_group(session: Session, group_id: str) -> Trip | None:
    return session.exec(select(Trip).where(Trip.line_group_id == group_id, Trip.status == "open")).first()


def ensure_member(session: Session, user_id: str, name: str | None) -> Member:
    query = insert(Member).values(line_user_id=user_id, line_name=name)
    if name:
        query = query.on_conflict_do_update(index_elements=["line_user_id"], set_={"line_name": name})
    else:
        query = query.on_conflict_do_nothing(index_elements=["line_user_id"])
    session.execute(query)
    return session.exec(select(Member).where(Member.line_user_id == user_id).execution_options(populate_existing=True)).one()


def enroll(session: Session, trip_id: int, member_id: int) -> None:
    session.execute(insert(TripMember).values(trip_id=trip_id, member_id=member_id).on_conflict_do_nothing())


def people(session: Session, trip_id: int) -> dict[int, tuple[TripMember, Member]]:
    rows = session.exec(select(TripMember, Member).join(Member, TripMember.member_id == Member.id).where(
        TripMember.trip_id == trip_id,
    ).order_by(TripMember.member_id)).all()
    return {membership.member_id: (membership, member) for membership, member in rows}


def name_of(people: dict, member_id: int) -> str:
    membership, member = people[member_id]
    return membership.nickname or member.line_name or f"成員 {member_id}"


def resolve(people: dict, speaker: int, name: str, mentions: dict[str, int]) -> int:
    if name in mentions:
        return mentions[name]
    if name in ("我", "自己"):
        return speaker
    key = normalize(name).lstrip("@").casefold()
    if key:
        for rank in ("nickname", "line_name", "contains"):
            matches = []
            for member_id, (membership, member) in people.items():
                nickname = normalize(membership.nickname or "").casefold()
                line_name = normalize(member.line_name or "").casefold()
                if ((rank == "nickname" and key == nickname) or (rank == "line_name" and key == line_name)
                    or (rank == "contains" and any(alias and (key in alias or alias in key)
                                                   for alias in (nickname, line_name)))):
                    matches.append(member_id)
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise CommandError(f"「{name}」對到多位成員，請使用完整名稱或 LINE 提及。")
    raise CommandError(f"找不到「{name}」。請查詢「成員」，或直接在群組提及對方。")


def quote_amount(trip: Trip, amount: Decimal, currency: str) -> tuple[float | None, float | None]:
    rate, value = split.convert(amount, currency, trip.settle_currency, fixed_rates=trip.fixed_rates)
    if rate is None:
        rate, value = split.convert(amount, currency, trip.settle_currency, rates=fx.rates())
    return (float(rate), float(value)) if rate is not None else (None, None)


def _memory_key(text: str) -> str:
    return "".join(normalize(text).casefold().split())


def _category(session: Session, item: str, merchant: str | None) -> tuple[str, str]:
    keys = dict.fromkeys(_memory_key(value) for value in (merchant, item) if value)
    for key in keys:
        remembered = session.get(CategoryMemory, key)
        if remembered:
            return remembered.category, "memory"
    for category, words in KEYWORDS.items():
        if any(word in key for key in keys for word in words.split()):
            return category, "keyword"
    return "待分類", "none"


def resolve_category(session: Session, item: str, merchant: str | None, suggested: str | None) -> tuple[str, str]:
    """記憶與關鍵字優先，對不到時才用 Gemini 建議的分類。確認卡片與入帳共用。"""
    detected, source = _category(session, item, merchant)
    if source == "none" and suggested in CATEGORIES and suggested != "待分類":
        return suggested, "gemini"
    return detected, source


def set_category(session: Session, expense: Expense, category: str) -> None:
    if category not in CATEGORIES:
        raise CommandError("類別不存在")
    expense.category, expense.category_source = category, "user"
    session.merge(CategoryMemory(key=_memory_key(expense.merchant or expense.item), category=category))


def expense_shares(session: Session, expense_id: int) -> list[ExpenseShare]:
    return list(session.exec(select(ExpenseShare).where(ExpenseShare.expense_id == expense_id)
                             .order_by(ExpenseShare.member_id)).all())


def replace_shares(session: Session, expense: Expense, shares: list[Share]) -> None:
    existing = {share.member_id: share for share in expense_shares(session, expense.id)}
    for share in shares:
        row = existing.pop(share.member_id, None)
        if row is None:
            session.add(ExpenseShare(expense_id=expense.id, **share.model_dump()))
        else:
            row.weight, row.amount = share.weight, share.amount
    for row in existing.values():
        session.delete(row)
    expense.split_mode = "exact" if shares[0].amount is not None else "equal"


def record_expense(session: Session, trip_id: int, *, item: str, amount: Decimal, currency: str,
                   quote: tuple[float | None, float | None], spent_on: date, payer_id: int,
                   creator_id: int, shares: list[Share], source_text: str | None,
                   source: str = "line_text", merchant: str | None = None, category: str | None = None,
                   kind: str = "expense") -> Expense:
    detected, category_source = resolve_category(session, item, merchant, category)
    entry = Expense(trip_id=trip_id, spent_on=spent_on, item=item, kind=kind, merchant=merchant, category=detected,
                    category_source=category_source, amount=float(amount), currency=currency,
                    rate=quote[0], amount_settle=quote[1], payer_id=payer_id, created_by=creator_id,
                    source=source, source_text=source_text)
    session.add(entry)
    session.flush()
    replace_shares(session, entry, shares)
    return entry


def expenses(session: Session, trip_id: int) -> list[Expense]:
    return list(session.exec(select(Expense).where(Expense.trip_id == trip_id, Expense.deleted_at.is_(None))
                             .order_by(Expense.spent_on.desc(), Expense.id.desc())))


def transfers(session: Session, trip_id: int) -> list[Transfer]:
    return list(session.exec(select(Transfer).where(Transfer.trip_id == trip_id, Transfer.deleted_at.is_(None))
                             .order_by(Transfer.created_at.desc(), Transfer.id.desc())))


def shares_by_expense(session: Session, trip_id: int) -> dict[int, list[ExpenseShare]]:
    shares = defaultdict(list)
    for share in session.exec(select(ExpenseShare).join(Expense, ExpenseShare.expense_id == Expense.id).where(
        Expense.trip_id == trip_id, Expense.deleted_at.is_(None),
    ).order_by(ExpenseShare.member_id)):
        shares[share.expense_id].append(share)
    return shares


def total(session: Session, trip: Trip) -> split.Settlement:
    return split.settlement(expenses(session, trip.id), shares_by_expense(session, trip.id),
                            transfers(session, trip.id), trip.settle_currency)


def suggested_payment(session: Session, trip: Trip, payment: split.Payment) -> bool:
    return payment in total(session, trip).payments


def fill_missing_rates() -> None:
    with Session(get_engine()) as session:
        for model in (Expense, Transfer):
            pending = session.exec(select(model, Trip).join(Trip, model.trip_id == Trip.id).where(
                model.amount_settle.is_(None), model.deleted_at.is_(None), Trip.deleted_at.is_(None),
            ).order_by(model.id).limit(100)).all()
            # 先查詢匯率，避免在網路請求期間佔住 SQLite 的寫入鎖。
            quotes = [(row, quote_amount(trip, Decimal(str(row.amount)), row.currency)) for row, trip in pending]
            for row, (rate, value) in quotes:
                if value is not None:
                    session.execute(update(model).where(
                        model.id == row.id, model.amount_settle.is_(None), model.deleted_at.is_(None),
                        model.amount == row.amount, model.currency == row.currency,
                    ).values(rate=rate, amount_settle=value))
            session.commit()
