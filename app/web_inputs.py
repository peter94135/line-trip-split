import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from unicodedata import normalize
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.services.trips import CATEGORIES

Amount = Annotated[Decimal, Field(gt=0, le=999_999_999_999, decimal_places=2)]
Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{3}$")]
Weight = Annotated[Decimal, Field(gt=0, le=100, decimal_places=4)]
Rate = Annotated[Decimal, Field(gt=0, le=1_000_000_000, decimal_places=12)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
MemberId = Annotated[int, Field(gt=0, le=9_223_372_036_854_775_807)]


class LineLogin(BaseModel):
    id_token: str = Field(min_length=1, max_length=8192)


class AdminLogin(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscription(BaseModel):
    endpoint: str = Field(pattern=r"^https://", max_length=2048)
    keys: PushKeys


class PushEndpoint(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


class JoinTrip(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{8}$")


class ShareInput(BaseModel):
    member_id: MemberId
    weight: Weight | None = None
    amount: Amount | None = None


class ExpenseInput(BaseModel):
    item: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    kind: Literal["expense", "refund"] = "expense"
    merchant: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    spent_on: date
    amount: Amount
    currency: Currency
    payer_id: MemberId
    category: str = "待分類"
    split_mode: Literal["equal", "exact"]
    shares: list[ShareInput] = Field(min_length=1, max_length=500)

    @field_validator("category")
    @classmethod
    def category_allowed(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError("類別不存在")
        return value

    @model_validator(mode="after")
    def split_fields(self):
        if len({row.member_id for row in self.shares}) != len(self.shares):
            raise ValueError("分攤成員重複")
        if self.split_mode == "exact" and any(row.amount is None or row.weight is not None for row in self.shares):
            raise ValueError("請填寫每位成員的分攤金額")
        if self.split_mode == "equal" and any(row.amount is not None for row in self.shares):
            raise ValueError("按權重分攤時請使用權重")
        return self


class BulkExpenseInput(BaseModel):
    source: Literal["web_ai", "web_photo", "web"] = "web_ai"
    text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    expenses: list[ExpenseInput] = Field(min_length=1, max_length=5)


class AiText(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class TransferInput(BaseModel):
    from_id: MemberId
    to_id: MemberId
    amount: Amount
    currency: Currency
    settlement_version: int | None = Field(default=None, ge=0)


class PaymentNoteInput(BaseModel):
    payment_note: str | None = Field(default=None, max_length=100)

    @field_validator("payment_note")
    @classmethod
    def mask_account_number(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = normalize("NFKC", value).strip()
        if (len(value) > 100 or not value.isprintable() or not any(char.isalpha() for char in value)
            or not re.fullmatch(r"[^\d]+[0-9]{0,4}", value)):
            raise ValueError("請只填收款平台與最多四位末碼，例如 LINE Pay 末碼 1234")
        return value


class MemberInput(BaseModel):
    nickname: Name | None = None
    weight: Weight = Decimal(1)
    active: bool = True


class ManualMember(BaseModel):
    nickname: Name
    weight: Weight = Decimal(1)


class TripSettings(BaseModel):
    name: Name
    start_date: date | None = None
    end_date: date | None = None
    timezone: str = Field(min_length=1, max_length=64)
    default_currency: Currency
    settle_currency: Currency
    fixed_rates: dict[Currency, Rate] = Field(default_factory=dict, max_length=100)
    notify_web_changes: bool | None = None
    default_payer_id: int | None = Field(default=None, ge=0)  # 0：記帳的人

    @model_validator(mode="after")
    def dates_and_timezone(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("結束日期需在開始日期之後")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("請輸入有效時區，例如 Asia/Taipei") from exc
        return self
