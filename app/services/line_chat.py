"""LINE 指令分派。所有帳本變更由收件匣在同一筆交易中提交。"""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlmodel import Session, select

from app.config import get_settings
from app.models import LineChat, LineDelivery, Member, Transfer, Trip, TripMember, utcnow
from app.services import gemini, line, line_draft, line_edit, line_settlement, trips
from app.services.line_reports import group_invite, read_command, reply, statistics_reply, trip_link
from app.services.line_parse import (
    RECORD, CommandError, Draft, IncomingText, incoming_text, is_command, money, normalize, parse_quick,
    parse_repayment, parse_trip,
)

HELP = (
    "旅伴分帳\n"
    "在群組傳「開帳本 京都 11/20-11/25 日幣」，旅伴可點回覆中的連結加入，也可傳「加入」。\n"
    "記帳：記 晚餐 3000\n"
    "收據：先傳「記」再傳照片，或先傳照片再說「記 這張」。\n"
    "指定付款與分攤：記 小明付 計程車 800 我和小華\n"
    "固定由某人付：預設付款人 小明（取消：預設付款人 取消）\n"
    "還款：已還 小明 1200｜公費：交公費 小明 5000\n"
    "退款：記 押金退回 5000 小明收\n"
    "查詢：帳本、成員、明細、結算、我欠多少、統計、我的統計\n"
    "旅行結束：結束帳本"
)
PHOTO_REQUEST = re.compile(r"^(?:這張(?:照片|收據|發票)?|照片|收據|發票)(?=$|[\s,:])")


def interested(event: dict) -> bool:
    source = event.get("source") or {}
    kind = event.get("type")
    if source.get("type") == "user" and source.get("userId"):
        return kind in ("follow", "message", "postback")
    if source.get("type") != "group" or not source.get("groupId"):
        return False
    if kind in ("join", "leave", "memberJoined", "postback"):
        return True
    message = event.get("message") or {}
    if kind != "message" or not source.get("userId"):
        return False
    if message.get("type") != "text":
        return message.get("type") == "image" and bool(get_settings().gemini_api_key)
    text = incoming_text(message)
    return (text.addressed or is_command(text.text) or text.text in line_draft.CONFIRM
            or line_draft.correction(text.text) is not None)


def _member(session: Session, user_id: str) -> Member | None:
    return session.exec(select(Member).where(Member.line_user_id == user_id)).first()


PROFILE_TTL = timedelta(hours=6)
_profile_cache: dict[tuple[str | None, str], tuple[datetime, str | None]] = {}


def _profile_name(user_id: str, group_id: str | None) -> str | None:
    # 每則群組指令都要確認發話者，LINE 名稱很少改，短時間內沿用上次查到的結果，少等一次 LINE API。
    key, now = (group_id, user_id), utcnow()
    if (cached := _profile_cache.get(key)) and now - cached[0] < PROFILE_TTL:
        return cached[1]
    name = line.get_profile(user_id, group_id).get("displayName")
    name = name if isinstance(name, str) and name.strip() else None
    if name:  # 查不到時不快取，下次再試。
        _profile_cache[key] = (now, name)
    return name


def _event_time(event: dict) -> datetime:
    try:
        return datetime.fromtimestamp(event["timestamp"] / 1000, UTC)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return utcnow()


def _enroll(session: Session, trip: Trip, user_id: str, group_id: str | None,
            mentioned: dict[str, tuple[str | None, str]]) -> tuple[int, dict[str, int]]:
    for target_id, label in mentioned.values():
        if not target_id:
            raise CommandError(f"LINE 沒有提供「{label}」的身分，請對方先傳「加入」，再用名稱分攤。")
    user_ids = dict.fromkeys([user_id, *(target_id for target_id, _ in mentioned.values())])
    names = {target_id: _profile_name(target_id, group_id) for target_id in user_ids}
    for target_id, label in mentioned.values():
        names[target_id] = names[target_id] or label
    members = {target_id: trips.ensure_member(session, target_id, name).id for target_id, name in names.items()}
    for member_id in members.values():
        trips.enroll(session, trip.id, member_id)
    return members[user_id], {key: members[target_id] for key, (target_id, _) in mentioned.items()}


def _open(session: Session, text: str, group_id: str | None, user_id: str, when: datetime) -> list[dict]:
    if group_id and (open_trip := trips.open_in_group(session, group_id)):
        return _already_open(open_trip)
    timezone = get_settings().tz
    name, start, end, currency = parse_trip(text, when.astimezone(ZoneInfo(timezone)).date())
    profile = _profile_name(user_id, group_id)
    if group_id:
        # 查 LINE 暱稱期間，網頁可能已重新開啟同群組的帳本。
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if open_trip := trips.open_in_group(session, group_id):
            return _already_open(open_trip)
    creator = trips.ensure_member(session, user_id, profile)
    trip = Trip(name=name, line_group_id=group_id, start_date=start, end_date=end,
                timezone=timezone, default_currency=currency, created_by=creator.id)
    session.add(trip)
    session.flush()
    trips.enroll(session, trip.id, creator.id)
    if group_id:
        join = group_invite(trip) or "\n請每位成員傳「加入」。"
    else:
        _select_private(session, user_id, trip.id)
        join = trip_link(trip, label="開啟帳本：")
    return reply(f"已開帳本「{trip.name}」｜記帳幣別 {trip.default_currency}｜結算幣別 {trip.settle_currency}"
                 f"{join}\n開始記帳可傳「記 晚餐 3000」。")


def _already_open(trip: Trip) -> list[dict]:
    return reply(f"群組已有進行中的「{trip.name}」。請先結束目前的帳本。" + group_invite(trip))


def _private_choices(session: Session, user_id: str, *, include_closed: bool = False) -> list[Trip]:
    query = select(Trip).join(TripMember, TripMember.trip_id == Trip.id).join(
        Member, Member.id == TripMember.member_id,
    ).where(Member.line_user_id == user_id, Trip.deleted_at.is_(None))
    if not include_closed:
        query = query.where(Trip.status == "open")
    return list(session.exec(query.order_by((Trip.status == "open").desc(), Trip.id.desc())).all())


def _select_private(session: Session, user_id: str, trip_id: int) -> None:
    chat = session.get(LineChat, user_id)
    if chat is None:
        session.add(LineChat(line_user_id=user_id, selected_trip_id=trip_id))
    else:
        chat.selected_trip_id = trip_id
        chat.updated_at = utcnow()


def _pick_private(choices: list[Trip]) -> list[dict]:
    if not choices:
        return reply("目前沒有進行中的帳本。請在旅伴群組傳「加入」，或傳「開帳本 京都」。")
    options = [(trip.name, f"trip={trip.id}") for trip in choices]
    return reply("請選擇帳本，也可輸入「帳本 帳本名稱」。\n" + "\n".join(trip.name for trip in choices), options)


def _persist(session: Session, trip: Trip, speaker: int, roster: dict, pending: line_draft.Pending,
             quotes: list[tuple[float | None, float | None]]) -> list[dict]:
    messages = []
    for draft, quoted in zip(pending.expenses, quotes, strict=True):
        currency, shares, payer_id = draft.currency, draft.shares, draft.payer_id
        entry = trips.record_expense(session, trip.id, item=draft.item, amount=draft.amount, currency=currency,
                                     quote=quoted, spent_on=draft.spent_on, payer_id=payer_id,
                                     creator_id=speaker, shares=shares, source_text=pending.text,
                                     source=pending.source, merchant=draft.merchant, category=draft.category,
                                     kind=draft.kind)
        messages.append(line_edit.expense_card(session, trip, entry, roster, "已記"))
    return messages


def _chat(session: Session, key: str) -> LineChat:
    chat = session.get(LineChat, key)
    if chat is None:
        chat = LineChat(line_user_id=key)
        session.add(chat)
    return chat


def _propose(session: Session, trip: Trip, group_id: str | None, user_id: str, key: str, incoming: IncomingText,
             raw: str, when: datetime, *, quick: list[Draft] | None = None, photo_id: str | None = None,
             previous: line_draft.Pending | None = None) -> list[dict]:
    today = when.astimezone(ZoneInfo(trip.timezone)).date()
    drafts, source = quick, "line_text"
    if get_settings().gemini_api_key:
        roster = trips.people(session, trip.id)
        members = [dict(nickname=membership.nickname, line_name=member.line_name, active=membership.active,
                        speaker=member.line_user_id == user_id) for membership, member in roster.values()]
        aliases = {f"提及{index}": marker for index, marker in enumerate(incoming.mentions, 1)}
        text = incoming.text
        for alias, marker in aliases.items():
            text = text.replace(marker, alias)
        try:
            image = line.get_content(photo_id, max_bytes=gemini.MAX_IMAGE_BYTES) if photo_id else None
            parsed = gemini.parse(text, today=today, currency=trip.default_currency, members=members,
                                  mentions={alias: incoming.mentions[marker][1] for alias, marker in aliases.items()},
                                  image=image, previous=line_draft.previous_entries(previous, roster) if previous else None)
        except line.LineError as exc:
            raise CommandError(str(exc)) from exc
        except gemini.GeminiError as exc:
            if drafts is None:
                raise CommandError(str(exc)) from exc
        else:
            source = "line_photo" if photo_id or (previous and previous.source == "line_photo") else "gemini"
            drafts = line_draft.from_gemini(parsed, photo=source == "line_photo", mention_tokens=aliases)
    if drafts is None:
        raise CommandError("目前可先用單筆文字記帳，例如「記 晚餐 3000」。")

    speaker, mentions = _enroll(session, trip, user_id, group_id, incoming.mentions)
    _require_open(session, trip)
    roster = trips.people(session, trip.id)
    entries = line_draft.prepare(drafts, trip, today, roster, speaker, mentions)
    chat = _chat(session, key)
    if photo_id:
        chat.listen_until, chat.listen_text, chat.listen_mentions = None, None, None
        if chat.recent_image_at and chat.recent_image_at <= when:
            chat.recent_image_id, chat.recent_image_at = None, None
    pending = line_draft.Pending(trip_id=trip.id, source=source, text=raw or None, expenses=entries)
    return line_draft.card(session, chat, pending, trip, roster)


def _record(session: Session, trip: Trip, group_id: str | None, user_id: str, key: str, incoming: IncomingText,
            raw: str, when: datetime) -> list[dict]:
    try:
        drafts = parse_quick(incoming.text)
    except CommandError as exc:
        if not get_settings().gemini_api_key:
            raise exc
        return _propose(session, trip, group_id, user_id, key, incoming, raw, when)
    if len(drafts) > line_draft.MAX_EXPENSES:
        raise CommandError("一次最多記五筆，請分開傳送。")
    if len(drafts) > 1:
        return _propose(session, trip, group_id, user_id, key, incoming, raw, when, quick=drafts)

    draft = drafts[0]
    currency = draft.currency or trip.default_currency
    settle_currency = trip.settle_currency
    quotes = [trips.quote_amount(trip, draft.amount, currency)]
    # 和查匯率一樣在寫入前呼叫，等 Gemini 時不佔資料庫寫入鎖。
    if draft.category is None and (guessed := gemini.guess_category(session, draft.item, draft.merchant)):
        drafts = [replace(draft, category=guessed)]
    speaker, mentions = _enroll(session, trip, user_id, group_id, incoming.mentions)
    _require_open(session, trip, settle_currency)
    roster = trips.people(session, trip.id)
    today = when.astimezone(ZoneInfo(trip.timezone)).date()
    entries = line_draft.prepare(drafts, trip, today, roster, speaker, mentions)
    chat = session.get(LineChat, key)
    if line_draft.get(chat):
        line_draft.clear(chat)
    return _persist(session, trip, speaker, roster, line_draft.Pending(
        trip_id=trip.id, source="line_text", text=raw, expenses=entries,
    ), quotes)


def _draft_action(session: Session, trip: Trip, actor: Member | None, group_id: str | None, user_id: str,
                  chat: LineChat, pending: line_draft.Pending, action: str, incoming: IncomingText,
                  when: datetime) -> list[dict]:
    if action == "cancel":
        line_draft.clear(chat)
        return reply("已取消這份記帳草稿。")
    if pending.trip_id != trip.id or trip.status != "open" or actor is None:
        raise CommandError("這份草稿的帳本已切換或結束，請重新記帳。")
    if action == "edit":
        text = line_draft.correction(incoming.text)
        if not text:
            return reply("請說明要修改的內容，例如「改成金額 1200」。")
        return _propose(session, trip, group_id, user_id, chat.line_user_id, incoming, text, when, previous=pending)

    settle_currency = trip.settle_currency
    quotes = [trips.quote_amount(trip, entry.amount, entry.currency) for entry in pending.expenses]
    line_draft.clear(chat)
    session.flush()
    _require_open(session, trip, settle_currency)
    roster = trips.people(session, trip.id)
    if actor.id not in roster or any(
        entry.payer_id not in roster or any(
            share.member_id not in roster or (not roster[share.member_id][0].active and share.member_id != entry.payer_id)
            for share in entry.shares
        ) for entry in pending.expenses
    ):
        raise CommandError("分攤成員有變動，請重新送出記帳指令。")
    return _persist(session, trip, actor.id, roster, pending, quotes)


def _photo(session: Session, trip: Trip, group_id: str | None, user_id: str, key: str, image_id: str,
           when: datetime) -> list[dict]:
    chat = session.get(LineChat, key)
    same_trip = chat and chat.photo_trip_id == trip.id
    if same_trip and chat.listen_until and chat.listen_until - line_draft.PHOTO_WAIT <= when <= chat.listen_until:
        prompt = chat.listen_text or "讀取這張收據"
        incoming = IncomingText(prompt, False, chat.listen_mentions or {})
        return _propose(session, trip, group_id, user_id, key, incoming, "", when,
                        photo_id=image_id)
    if not same_trip or not chat.recent_image_at or when >= chat.recent_image_at:
        chat = _chat(session, key)
        if not same_trip:
            chat.photo_trip_id = trip.id
            chat.listen_until, chat.listen_text, chat.listen_mentions = None, None, None
        chat.recent_image_id, chat.recent_image_at = image_id, when
    return []


def _photo_request(session: Session, trip: Trip, group_id: str | None, user_id: str, key: str,
                   incoming: IncomingText, when: datetime) -> list[dict] | None:
    command = RECORD.match(incoming.text)
    if not command:
        return None
    body = incoming.text[command.end():]
    use_recent = PHOTO_REQUEST.match(body) is not None
    if body and not use_recent:
        return None
    if not get_settings().gemini_api_key:
        return reply("目前可先用文字記帳，例「記 晚餐 3000」。")
    chat = session.get(LineChat, key)
    if use_recent and chat and chat.photo_trip_id == trip.id and chat.recent_image_id and chat.recent_image_at and (
        timedelta(0) <= when - chat.recent_image_at <= line_draft.RECENT_PHOTO
    ):
        return _propose(session, trip, group_id, user_id, key, incoming, incoming.text, when,
                        photo_id=chat.recent_image_id)
    chat = _chat(session, key)
    if chat.photo_trip_id != trip.id:
        chat.photo_trip_id = trip.id
        chat.recent_image_id, chat.recent_image_at = None, None
    chat.listen_until, chat.listen_text = when + line_draft.PHOTO_WAIT, incoming.text if use_recent else None
    chat.listen_mentions = incoming.mentions or None
    return reply("請在 2 分鐘內傳收據照片，我會先回覆待確認的明細。")


def _repay(session: Session, trip: Trip, group_id: str | None, user_id: str, incoming: IncomingText) -> list[dict]:
    name, amount, currency = parse_repayment(incoming.text)
    currency = currency or trip.settle_currency
    settle_currency = trip.settle_currency
    rate, value = trips.quote_amount(trip, amount, currency)
    speaker, mentions = _enroll(session, trip, user_id, group_id, incoming.mentions)
    _require_open(session, trip, settle_currency)
    roster = trips.people(session, trip.id)
    recipient = trips.resolve(roster, speaker, name, mentions)
    if recipient == speaker:
        raise CommandError("還款對象必須是另一位帳本成員。")
    session.add(Transfer(trip_id=trip.id, from_id=speaker, to_id=recipient, amount=float(amount),
                         currency=currency, rate=rate, amount_settle=value, created_by=speaker, source="line_text"))
    pending = "（待換算）" if value is None else ""
    if incoming.text == "交公費" or incoming.text.startswith("交公費 "):
        return reply(f"已記：你交給{trips.name_of(roster, recipient)}公費 {money(amount, currency)}{pending}")
    return reply(f"已記還款：{trips.name_of(roster, speaker)} → {trips.name_of(roster, recipient)} "
                 f"{money(amount, currency)}{pending}")


def _require_open(session: Session, trip: Trip, settle_currency: str | None = None) -> None:
    # 已取得寫入鎖，確認別的對話沒有在查匯率或名稱期間關閉帳本。
    session.refresh(trip, attribute_names=["status", "settle_currency"])
    if trip.status != "open":
        raise CommandError("這本帳已結束。傳「開帳本 旅程名稱」可開新帳本。")
    if settle_currency is not None and trip.settle_currency != settle_currency:
        raise CommandError("帳本的結算幣別剛變更，請重新送出指令。")


def _default_payer(session: Session, trip: Trip, actor: Member | None, group_id: str | None, user_id: str,
                   incoming: IncomingText) -> list[dict]:
    name = incoming.text.removeprefix("預設付款人").strip()
    if not name:
        roster = trips.people(session, trip.id)
        if trip.default_payer_id in roster:
            return reply(f"預設付款人：{trips.name_of(roster, trip.default_payer_id)}。沒指定付款人的帳都記在他身上。")
        return reply("預設付款人：記帳的人。傳「預設付款人 小明」可改成沒指定時都記小明付。")
    # 先確認權限，避免非成員只因嘗試設定就加入帳本。
    if (not actor or trip.created_by != actor.id) and get_settings().line_admin_user_id != user_id:
        raise CommandError("請由帳本建立者或管理員設定預設付款人。")
    speaker, mentions = _enroll(session, trip, user_id, group_id, incoming.mentions)
    _require_open(session, trip)
    roster = trips.people(session, trip.id)
    if name in ("取消", "記帳的人", "無"):
        trip.default_payer_id, label = None, "記帳的人"
    else:
        member_id = trips.resolve(roster, speaker, name, mentions)
        if not roster[member_id][0].active:
            raise CommandError("這位成員已停用分攤，不能設為預設付款人。")
        trip.default_payer_id, label = member_id, trips.name_of(roster, member_id)
    trips.audit(session, trip.id, speaker, "line", "設定預設付款人", label)
    tail = "" if trip.default_payer_id is None else "要記成自己付時傳「記 我付 晚餐 300」。"
    return reply(f"預設付款人改為{label}。{tail}")


def handle(session: Session, event: dict) -> list[dict]:
    group = (event.get("source") or {}).get("groupId")
    message = event.get("message") or {}
    text = incoming_text(message).text if message.get("type") == "text" else ""
    # 只在開帳和查名單時提醒；記帳時每筆都提醒太吵，也會擠掉第 5 張卡片。
    check = text in ("加入", "成員", "帳本") or text.startswith("開帳本")
    count = line.group_count(group) if group and check else None
    messages = _handle_event(session, event)
    if count is not None and 0 < len(messages) < 5 and (trip := trips.open_in_group(session, group)):
        warning = trips.group_warning(trips.people(session, trip.id), count)
        if warning:
            # 放在卡片前，保留最後一則訊息的 Quick Reply。
            messages.insert(0, line.text_message(warning))
    return messages


def _handle_event(session: Session, event: dict) -> list[dict]:
    source = event.get("source") or {}
    group_id, user_id = source.get("groupId"), source.get("userId")
    kind = event.get("type")
    if kind in ("join", "follow"):
        return reply(HELP)
    if kind == "memberJoined":
        return reply("歡迎加入群組。已有帳本時，傳「加入」即可參與分攤；傳「說明」查看用法。")
    if kind == "leave" and group_id:
        session.execute(update(LineDelivery).where(LineDelivery.recipient == group_id, LineDelivery.status == "pending")
                        .values(status="failed", messages=None))
        return []
    if not user_id:
        return []
    if kind == "postback":
        return _postback(session, event, group_id, user_id)
    return _message(session, event, group_id, user_id, event.get("message") or {})


def _postback(session: Session, event: dict, group_id: str | None, user_id: str) -> list[dict]:
    data = {key: values[0] for key, values in parse_qs((event.get("postback") or {}).get("data", "")).items()}
    if "settle" in data:
        return [line_settlement.handle(session, group_id, user_id, data)]
    if "exp" in data:
        return line_edit.handle(session, group_id, user_id, data)
    if not group_id and "trip" in data:
        choices = _private_choices(session, user_id, include_closed=True)
        match = next((trip for trip in choices if str(trip.id) == data["trip"]), None)
        if not match:
            return _pick_private(choices)
        _select_private(session, user_id, match.id)
        return reply(f"已選擇「{match.name}」。")
    if data.get("draft") in ("confirm", "cancel"):
        key = f"{group_id}:{user_id}" if group_id else user_id
        return _pending_action(session, key, group_id, user_id, IncomingText("", False, {}), _event_time(event),
                               data["draft"], nonce=data.get("nonce", ""))
    if data.get("cmd") == "members":
        return _message(session, event, group_id, user_id, {"type": "text", "text": "成員"})
    return []


def _message(session: Session, event: dict, group_id: str | None, user_id: str, message: dict) -> list[dict]:
    image_id = message.get("id") if message.get("type") == "image" else None
    if message.get("type") != "text" and not image_id:
        return [] if group_id else reply("請用文字或收據照片記帳。傳「說明」查看用法。")
    incoming = IncomingText("", False, {}) if image_id else incoming_text(message)
    text = incoming.text
    if text == "說明":
        return reply(HELP)
    if text.lower() == "我的id":
        return reply("請私訊我傳「我的ID」。" if group_id else f"你的 LINE User ID：\n{user_id}")
    if text == "開帳本" or text.startswith("開帳本 "):
        return _open(session, text, group_id, user_id, _event_time(event))

    key = f"{group_id}:{user_id}" if group_id else user_id
    action = line_draft.CONFIRM.get(text)
    if not action and line_draft.correction(text) is not None:
        action = "edit"
    if action:
        return _pending_action(session, key, group_id, user_id, incoming, _event_time(event), action)

    statistics = text in ("統計", "我的統計")
    if statistics:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    actor = _member(session, user_id)
    if group_id:
        trip = session.exec(select(Trip).where(Trip.line_group_id == group_id, Trip.deleted_at.is_(None))
                            .order_by((Trip.status == "open").desc(), Trip.id.desc()).limit(1)).first()
        if trip is None:
            return [] if image_id else reply("這個群組還沒有帳本。傳「開帳本 京都 11/20-11/25 日幣」即可建立。")
        membership = session.get(TripMember, (trip.id, actor.id)) if actor else None
        if not image_id and not is_command(text) and not membership:
            return reply("請傳「加入」加入這本帳，再開始記帳。")
    else:
        include_closed = statistics or text in ("結算", "我欠多少") or text.startswith("帳本 ")
        choices = _private_choices(session, user_id, include_closed=include_closed)
        if text.startswith("帳本 "):
            name = normalize(text.removeprefix("帳本 "))
            matches = [trip for trip in choices if normalize(trip.name) == name]
            if len(matches) != 1:
                return _pick_private(choices)
            _select_private(session, user_id, matches[0].id)
            return reply(f"已選擇「{matches[0].name}」。")
        chat = session.get(LineChat, user_id)
        trip = choices[0] if len(choices) == 1 else next(
            (item for item in choices if chat and item.id == chat.selected_trip_id), None,
        )
        if trip is None:
            return [] if image_id else _pick_private(choices)
        membership = session.get(TripMember, (trip.id, actor.id))

    when = _event_time(event)
    if image_id:
        if trip.status != "open" or not get_settings().gemini_api_key:
            return []
        return _photo(session, trip, group_id, user_id, key, image_id, when)

    if statistics:
        if not membership:
            return reply("請先傳「加入」加入帳本，再查看統計。" if trip.status == "open"
                         else "這本帳已結束，請由原帳本成員查詢。")
        return statistics_reply(session, trip, actor.id, personal=text == "我的統計")

    reading = text in ("帳本", "成員", "明細", "結算", "我欠多少", "結束帳本", "加入")
    if trip.status == "closed":
        if not reading or text == "加入":
            return reply("這本帳已結束。傳「開帳本 旅程名稱」可開新帳本。")
        if not membership:
            return reply("這本帳已結束，請由原帳本成員查詢。")
        return read_command(session, trip, actor.id, user_id, text)
    if reading:
        # 只有建立者與管理員能結束帳本，其他人不會因嘗試結束而加入。
        if text == "結束帳本" and not membership and get_settings().line_admin_user_id != user_id:
            raise CommandError("請由帳本建立者或管理員結束帳本。")
        speaker, _ = _enroll(session, trip, user_id, group_id, {})
        return read_command(session, trip, speaker, user_id, text)
    if text in ("已還", "交公費") or text.startswith(("已還 ", "交公費 ")):
        return _repay(session, trip, group_id, user_id, incoming)
    if text == "預設付款人" or text.startswith("預設付款人 "):
        return _default_payer(session, trip, actor, group_id, user_id, incoming)
    if RECORD.match(text) or (text and (not group_id or incoming.addressed)):
        photo = _photo_request(session, trip, group_id, user_id, key, incoming, when)
        if photo is not None:
            return photo
        return _record(session, trip, group_id, user_id, key, incoming, message["text"], when)
    return reply(HELP) if incoming.addressed else []


def _pending_action(session: Session, key: str, group_id: str | None, user_id: str, incoming: IncomingText,
                    when: datetime, action: str, nonce: str | None = None) -> list[dict]:
    chat = session.get(LineChat, key)
    pending = line_draft.get(chat)
    if not pending or (nonce is not None and chat.draft_nonce != nonce):
        return reply("這份記帳草稿已失效。") if nonce is not None else []
    draft_trip = session.get(Trip, pending.trip_id)
    if not draft_trip or draft_trip.deleted_at is not None or (group_id and draft_trip.line_group_id != group_id):
        raise CommandError("找不到這份草稿的帳本，請重新記帳。")
    if not group_id and chat.selected_trip_id not in (None, draft_trip.id):
        raise CommandError("這份草稿屬於另一個帳本，請先選回該帳本。")
    return _draft_action(session, draft_trip, _member(session, user_id), group_id, user_id, chat, pending,
                         action, incoming, when)
