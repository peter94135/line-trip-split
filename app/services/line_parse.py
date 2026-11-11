"""LINE 文字指令的確定性解析。"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

CURRENCIES = {
    "TWD": ("$", "台幣", "新台幣", "臺幣", "NT$", "NTD"),
    "JPY": ("¥", "日幣", "日圓", "日元", "円"),
    "USD": ("US$", "美金", "美元"), "EUR": ("€", "歐元"),
    "KRW": ("₩", "韓元", "韓幣"), "HKD": ("HK$", "港幣"),
    "CNY": ("CN¥", "人民幣", "RMB"), "THB": ("฿", "泰銖"),
    "SGD": ("S$", "新幣"), "GBP": ("£", "英鎊"), "VND": ("₫", "越南盾"),
    "AUD": ("A$", "澳幣"), "CAD": ("CA$", "加幣"), "MYR": ("RM", "馬幣"),
}
CURRENCY_WORDS = {word.upper(): code for code, words in CURRENCIES.items() for word in (code, *words)}
CURRENCY_PATTERN = "|".join(re.escape(word) for word in sorted(CURRENCY_WORDS, key=len, reverse=True))
AMOUNT = r"(?<![\d.,+\-])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.,])"
_ITEM_FIRST = re.compile(
    rf"(?P<item>.+?)\s*(?P<pre>{CURRENCY_PATTERN})?\s*(?P<amount>{AMOUNT})"
    rf"\s*(?P<post>{CURRENCY_PATTERN}|元|塊錢|塊)?(?P<tail>\s+\S.*|[、].*)?", re.I,
)
_AMOUNT_FIRST = re.compile(
    rf"(?P<pre>{CURRENCY_PATTERN})?\s*(?P<amount>{AMOUNT})"
    rf"\s*(?P<post>{CURRENCY_PATTERN}|元|塊錢|塊)?\s+(?P<item>\S.*)", re.I,
)
_MULTI = re.compile(r"[、;\n]+")
_NAMES = re.compile(r"\s*[,、和跟與]\s*|\s+(?=[\ue000-\uf8ff])|(?<=[\ue000-\uf8ff])\s+")
_EXACT = re.compile(rf"(.+?)\s+({AMOUNT})")
_DATE = r"(?:\d{4}[-/])?\d{1,2}[-/]\d{1,2}"
_TRIP_RANGE = re.compile(rf"(?P<name>.+?)\s+(?P<start>{_DATE})\s*[-~–—至]\s*(?P<end>{_DATE})$")
_REFUND = re.compile(r"退款|退費|退稅|押金退(?:回|還)|退還押金")
_REFUND_RECEIVER = re.compile(r"(?P<receiver>\S+?)\s*收(?:到|款)?(?:\s+(?P<tail>.+))?")
RECORD = re.compile(r"^記(?:帳)?(?:[\s:]+|$)")
COMMANDS = {"結算", "帳本", "明細", "成員", "加入", "說明", "我的id", "結束帳本", "我欠多少", "統計", "我的統計"}


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Draft:
    item: str
    amount: Decimal
    currency: str | None
    days_ago: int
    payer: str | None
    include: list[str] | None
    exclude: list[str]
    exact: list[tuple[str, Decimal]]
    treat: bool
    merchant: str | None = None
    category: str | None = None
    spent_on: date | None = None
    kind: Literal["expense", "refund"] = "expense"


@dataclass(frozen=True)
class IncomingText:
    text: str
    addressed: bool
    mentions: dict[str, tuple[str | None, str]]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def incoming_text(message: dict) -> IncomingText:
    text = message.get("text", "")
    offsets = {0: 0}
    offset = 0
    for i, char in enumerate(text, 1):
        offset += 1 if ord(char) <= 0xFFFF else 2
        offsets[offset] = i
    replacements = []
    mentions = {}
    addressed = False
    for entry in (message.get("mention") or {}).get("mentionees", []):
        if entry.get("type") != "user":
            continue
        start, length = entry.get("index"), entry.get("length")
        if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
            continue
        first, last = offsets.get(start), offsets.get(start + length)
        if first is None or last is None or not text[first:last].startswith("@"):
            continue
        if entry.get("isSelf"):
            addressed = True
            replacements.append((first, last, " "))
        else:
            marker = chr(0xE000 + len(mentions))
            mentions[marker] = (entry.get("userId"), text[first + 1:last])
            replacements.append((first, last, f" {marker} "))
    for first, last, replacement in sorted(replacements, reverse=True):
        text = text[:first] + replacement + text[last:]
    return IncomingText(normalize(text), addressed, mentions)


def is_command(text: str) -> bool:
    return text.lower() in COMMANDS or RECORD.match(text) is not None or any(
        text == prefix or text.startswith(f"{prefix} ") for prefix in ("開帳本", "已還", "交公費", "帳本")
    )


def _decimal(text: str) -> Decimal:
    value = Decimal(text.replace(",", ""))
    if not 0 < value <= 999_999_999_999:
        raise CommandError("金額必須大於 0，且小於一兆")
    return value


def _names(text: str) -> list[str]:
    names = [name.strip() for name in _NAMES.split(text.strip()) if name.strip()]
    if not names:
        raise CommandError("請填寫分攤成員")
    return names


def _currency(match: re.Match) -> str | None:
    currencies = {CURRENCY_WORDS[word.upper()] for word in (match["pre"], match["post"])
                  if word and word.upper() in CURRENCY_WORDS and word != "$"}
    if len(currencies) > 1:
        raise CommandError("同一筆金額只能有一個幣別")
    return next(iter(currencies), None)


def _single(text: str) -> Draft:
    days_ago = 0
    for day, offset in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if text.startswith(day):
            text, days_ago = text[len(day):].lstrip(), offset
            break
    omit_speaker = text.startswith("幫他們付 ")
    if omit_speaker:
        text = text.removeprefix("幫他們付 ").strip()
    prefix = re.match(r"^([^,、;]+?)\s*付(?:了)?\s+", text)
    if prefix and re.search(r"\s\d", prefix[1]):
        prefix = None
    payer = prefix[1].strip() if prefix else None
    if prefix:
        text = text[prefix.end():]
    treat = text.startswith(("我請客 ", "我出 "))
    if treat:
        text = text.split(maxsplit=1)[1]

    prefix_amount = _AMOUNT_FIRST.fullmatch(text)
    match = prefix_amount or _ITEM_FIRST.fullmatch(text)
    if not match:
        raise CommandError("請用「記 晚餐 3000」或「記 小明付 計程車 800 我和小華」")
    item = match["item"].strip(" :、,")
    tail = (match.groupdict().get("tail") or "").strip()
    if prefix_amount:
        marker = re.search(r"\s+(?=[\ue000-\uf8ff]|除了|我和|我跟|我不算|我請客|我出)", item)
        if marker:
            item, tail = item[:marker.start()], item[marker.end():]
    if (not item or len(item) > 100 or item.endswith(("-", "+", ".")) or re.search(
        r"[?,、;\ue000-\uf8ff]|(?:花了|多少|怎麼|是不是|要不要|可以嗎|請問|明天|後天)", item,
    )):
        raise CommandError("請確認品項與金額，例「記 晚餐 3000」")
    kind = "refund" if _REFUND.search(item) else "expense"
    if kind == "refund" and (receiver := _REFUND_RECEIVER.fullmatch(tail)):
        if payer:
            raise CommandError("退款請只指定一位收款人，例「記 押金退回 5000 小明收」")
        payer, tail = receiver["receiver"], receiver["tail"] or ""
    include, exclude, exact = None, ["我"] if omit_speaker else [], []
    if tail in ("我請客", "我出"):
        treat = True
    elif tail.startswith("除了"):
        exclude = _names(tail[2:])
    elif tail:
        omit_me = re.search(r"(?:^|\s)(?:我不算|幫他們付)(?:$|\s)", tail)
        if omit_me:
            tail = (tail[:omit_me.start()] + " " + tail[omit_me.end():]).strip()
            exclude = ["我"]
        if tail:
            amounts = [_EXACT.fullmatch(part.strip()) for part in re.split(r"[、,](?!\d)", tail)]
            if all(amounts):
                exact = [(amount[1], _decimal(amount[2])) for amount in amounts]
            else:
                include = _names(tail)
    return Draft(item, _decimal(match["amount"]), _currency(match), days_ago, payer,
                 include, exclude, exact, treat, kind=kind)


def parse_quick(text: str) -> list[Draft]:
    text = RECORD.sub("", normalize(text), count=1)
    if text in ("", "這張", "這張照片", "照片"):
        raise CommandError("目前可先用文字記帳，例「記 晚餐 3000」")
    parts = _MULTI.split(text)
    if len(parts) > 1:
        try:
            drafts = [_single(part.strip()) for part in parts]
        except CommandError:
            pass
        else:
            if all(not draft.include and not draft.exclude and not draft.exact for draft in drafts):
                return drafts
    return [_single(text)]


def parse_repayment(text: str) -> tuple[str, Decimal, str | None]:
    match = _ITEM_FIRST.fullmatch(re.sub(r"^(?:已還|交公費)(?:\s+|$)", "", text).strip())
    if not match or match["tail"] or not (name := match["item"].strip()) or name.endswith(("-", "+", ".")):
        raise CommandError("請用「已還 小明 1200」或「交公費 小明 5000」。")
    return name, _decimal(match["amount"]), _currency(match)


def parse_trip(text: str, today: date) -> tuple[str, date | None, date | None, str]:
    text = text.removeprefix("開帳本").strip()
    currency = "TWD"
    words = text.rsplit(maxsplit=1)
    if len(words) == 2 and words[1].upper() in CURRENCY_WORDS:
        text, currency = words[0], CURRENCY_WORDS[words[1].upper()]
    start = end = None
    match = _TRIP_RANGE.fullmatch(text)
    if match:
        def parse_day(raw: str, year: int) -> date:
            numbers = list(map(int, re.split(r"[-/]", raw)))
            return date(*(numbers if len(numbers) == 3 else [year, *numbers]))

        try:
            start = parse_day(match["start"], today.year)
            end = parse_day(match["end"], start.year)
            if end < start and len(re.split(r"[-/]", match["end"])) == 2:
                end = parse_day(match["end"], start.year + 1)
            if end < start:
                raise ValueError
        except ValueError as exc:
            raise CommandError("日期有誤，例「開帳本 京都 11/20-11/25 日幣」") from exc
        text = match["name"]
    if not text or len(text) > 80:
        raise CommandError("請輸入帳本名稱，例「開帳本 京都 11/20-11/25 日幣」")
    return text, start, end, currency


def money(amount: float | Decimal, currency: str) -> str:
    symbol = CURRENCIES.get(currency, (f"{currency} ",))[0]
    value = Decimal(str(amount))
    return f"{symbol}{value:,.0f}" if value == value.to_integral_value() else f"{symbol}{value:,.2f}"
