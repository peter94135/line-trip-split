"""旅伴分帳資料表。時間戳記在 SQLite 以 UTC 儲存，讀出時帶 UTC 時區。"""

import secrets
from datetime import UTC, date, datetime

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, String, text
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("時間必須包含時區")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def _invite_code() -> str:
    return secrets.token_urlsafe(6)


class Member(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    line_user_id: str | None = Field(default=None, unique=True, index=True)
    line_name: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Trip(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')"),
        Index("uq_trip_open_group", "line_group_id", unique=True,
              sqlite_where=text("status = 'open' AND line_group_id IS NOT NULL")),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str
    line_group_id: str | None = Field(default=None, index=True)
    status: str = "open"
    start_date: date | None = None
    end_date: date | None = None
    timezone: str = "Asia/Taipei"
    default_currency: str = "TWD"
    settle_currency: str = "TWD"
    fixed_rates: dict[str, float] | None = Field(default=None, sa_column=Column(JSON))
    invite_code: str = Field(default_factory=_invite_code, unique=True)
    ledger_version: int = 0
    # 網頁改帳時推播到 LINE 群組，會用到每月推播額度。
    notify_web_changes: bool = Field(default=True, sa_column_kwargs={"server_default": text("1")})
    # 沒說誰付錢時的付款人；NULL 表示記帳的人。例如一人保管公費時設成他。
    default_payer_id: int | None = Field(default=None, foreign_key="member.id")
    created_by: int | None = Field(default=None, foreign_key="member.id")
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    deleted_at: datetime | None = Field(default=None, sa_type=UTCDateTime)


class TripMember(SQLModel, table=True):
    trip_id: int = Field(primary_key=True, foreign_key="trip.id")
    member_id: int = Field(primary_key=True, foreign_key="member.id")
    nickname: str | None = None
    weight: float = 1.0
    active: bool = True
    payment_note: str | None = Field(default=None, max_length=100)
    joined_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Expense(SQLModel, table=True):
    __table_args__ = (
        ForeignKeyConstraint(["trip_id", "payer_id"], ["tripmember.trip_id", "tripmember.member_id"]),
        CheckConstraint("split_mode IN ('equal', 'exact')"),
    )

    id: int | None = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True)
    spent_on: date
    item: str
    kind: str = Field(default="expense", sa_column=Column(
        String, CheckConstraint("kind IN ('expense', 'refund')"), nullable=False, default="expense",
    ))
    merchant: str | None = None
    category: str
    category_source: str
    amount: float
    currency: str
    rate: float | None = None
    amount_settle: float | None = None
    payer_id: int
    split_mode: str = "equal"
    created_by: int | None = Field(default=None, foreign_key="member.id")
    source: str
    source_text: str | None = None
    deleted_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    deleted_by: int | None = Field(default=None, foreign_key="member.id")
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Share(SQLModel):
    member_id: int = Field(primary_key=True, foreign_key="member.id")
    weight: float | None = None
    amount: float | None = None


class ExpenseShare(Share, table=True):
    expense_id: int = Field(primary_key=True, foreign_key="expense.id")


class Transfer(SQLModel, table=True):
    source: str | None = None
    __table_args__ = (
        ForeignKeyConstraint(["trip_id", "from_id"], ["tripmember.trip_id", "tripmember.member_id"]),
        ForeignKeyConstraint(["trip_id", "to_id"], ["tripmember.trip_id", "tripmember.member_id"]),
        CheckConstraint("from_id != to_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    trip_id: int = Field(foreign_key="trip.id", index=True)
    from_id: int
    to_id: int
    amount: float
    currency: str
    rate: float | None = None
    amount_settle: float | None = None
    created_by: int | None = Field(default=None, foreign_key="member.id")
    deleted_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class AuditLog(SQLModel, table=True):
    """會改變帳目或帳本狀態的操作，給管理頁查誰在何時做了什麼。"""

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)
    trip_id: int | None = Field(default=None, foreign_key="trip.id", index=True)
    actor_id: int | None = Field(default=None, foreign_key="member.id")
    channel: str
    action: str
    detail: str = ""


class WebAiRequest(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    member_id: int = Field(foreign_key="member.id", index=True)
    at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime, index=True)


class CategoryMemory(SQLModel, table=True):
    key: str = Field(primary_key=True)
    category: str
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class LineInbox(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    event_key: str = Field(unique=True)
    actor: str = Field(index=True)
    payload: dict | None = Field(default=None, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)
    received_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class LineDelivery(SQLModel, table=True):
    id: str = Field(primary_key=True)  # 同時作為 LINE retry key
    recipient: str
    messages: list[dict] | None = Field(default=None, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)
    attempts: int = 0
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    next_attempt_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class LineChat(SQLModel, table=True):
    line_user_id: str = Field(primary_key=True)  # 群組內使用 groupId:userId
    selected_trip_id: int | None = None
    draft: dict | None = Field(default=None, sa_column=Column(JSON))
    draft_nonce: str | None = None
    draft_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    photo_trip_id: int | None = None
    listen_until: datetime | None = Field(default=None, sa_type=UTCDateTime)
    listen_text: str | None = None
    listen_mentions: dict | None = Field(default=None, sa_column=Column(JSON))
    recent_image_id: str | None = None
    recent_image_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class GeminiCall(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True, sa_type=UTCDateTime)
    model: str
    kind: str
    input_tokens: int = 0
    audio_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cost_usd: float | None = None


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
