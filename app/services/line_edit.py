"""在 LINE 用按鈕編輯已存支出，每次修改後回覆最新內容。"""

import secrets
from datetime import timedelta

from sqlmodel import Session, select

from app.models import Expense, LineChat, Member, Trip, TripMember, utcnow
from app.services import fx, line, split, trips
from app.services.line_parse import CommandError, money

PAGE_SIZE = 10
DRAFT_TTL = timedelta(minutes=15)


def _button(label: str, action: str, expense_id: int, **params) -> line.Button:
    data = f"exp={action}&id={expense_id}"
    return label, data + "".join(f"&{key}={value}" for key, value in params.items())


def _reply(text: str, buttons: list[line.Button] | None = None) -> list[dict]:
    return [line.text_message(text, buttons)]


def expense_card(session: Session, trip: Trip, entry: Expense, roster: dict, title: str,
                 *, show_market_conversion: bool = True) -> dict:
    """支出的最新狀態，附固定的編輯按鈕。記帳與每次修改後都回這張卡片。"""
    amount = money(entry.amount, entry.currency)
    attribution = ""
    if entry.amount_settle is None:
        amount += "（待換算）"
    elif entry.currency != trip.settle_currency and (show_market_conversion or entry.currency in (trip.fixed_rates or {})):
        amount += f"（約 {money(entry.amount_settle, trip.settle_currency)}）"
        if entry.currency not in (trip.fixed_rates or {}):
            attribution = f"\n{fx.ATTRIBUTION} {fx.ATTRIBUTION_URL}"
    names = []
    kind_label, verb = ("退款｜", "收") if entry.kind == "refund" else ("", "付")
    shares = trips.expense_shares(session, entry.id)
    for share in shares:
        name = trips.name_of(roster, share.member_id)
        if share.amount is not None:
            name += f" {money(share.amount, entry.currency)}"
        elif share.weight is not None and share.weight != 1:
            name += f" ×{share.weight:g}"
        names.append(name)
    buttons = [_button(label, action, entry.id) for action, label in (
        ("split", "改分攤"), ("payer", "改收款人" if entry.kind == "refund" else "改付款人"), ("cat", "改類別"), ("del", "刪除"),
    )]
    return line.card_message(
        f"{title}：{kind_label}{entry.item} {amount}｜{entry.category}\n{trips.name_of(roster, entry.payer_id)}{verb}｜"
        f"分給 {len(shares)} 人：{'、'.join(names)}{attribution}", buttons,
    )


def deleted_expense_card(entry: Expense, actor_name: str) -> dict:
    return line.card_message(f"{actor_name} 已刪除「{entry.item}」。", [_button("復原", "undo", entry.id)], columns=1)


def _active(roster: dict) -> list[int]:
    return [member_id for member_id, (membership, _) in roster.items() if membership.active]


def _page(data: dict, members: list[int]) -> int:
    value = data.get("page", "0")
    return min(int(value), max((len(members) - 1) // PAGE_SIZE, 0)) if value.isascii() and value.isdigit() and len(value) <= 6 else 0


def _navigation(buttons: list[line.Button], action: str, expense_id: int, page: int, count: int, **params):
    if page:
        buttons.append(_button("上一頁", action, expense_id, page=page - 1, **params))
    if (page + 1) * PAGE_SIZE < count:
        buttons.append(_button("下一頁", action, expense_id, page=page + 1, **params))


def _split_picker(expense: Expense, roster: dict, draft: dict, nonce: str, page: int) -> list[dict]:
    active = _active(roster)
    selected = draft["members"]
    buttons = []
    for member_id in active[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        mark = "✓ " if member_id in selected else "＋ "
        buttons.append(_button(mark + trips.name_of(roster, member_id), "toggle", expense.id,
                               who=member_id, nonce=nonce, page=page))
    _navigation(buttons, "splitpage", expense.id, page, len(active), nonce=nonce)
    buttons.append(_button("完成", "splitdone", expense.id, nonce=nonce))
    names = "、".join(trips.name_of(roster, member_id) for member_id in selected if member_id in roster) or "無"
    note = "\n完成後會改為按成員權重分攤。" if expense.split_mode == "exact" else ""
    return _reply(f"改分攤｜{expense.item}\n已選：{names}{note}", buttons)


def _edit_split(session: Session, trip: Trip, expense: Expense, roster: dict, chat_key: str, data: dict,
                actor_id: int) -> list[dict]:
    active = _active(roster)
    page = _page(data, active)
    chat = session.get(LineChat, chat_key)
    if data["exp"] == "split":
        selected = [row.member_id for row in trips.expense_shares(session, expense.id) if row.member_id in active]
        draft = {"expense": expense.id, "members": selected}
        nonce = secrets.token_urlsafe(8)
        if chat is None:
            chat = LineChat(line_user_id=chat_key)
            session.add(chat)
        chat.draft, chat.draft_nonce, chat.draft_at = draft, nonce, utcnow()
        return _split_picker(expense, roster, draft, nonce, page)

    if (not chat or not chat.draft or chat.draft.get("expense") != expense.id
        or chat.draft_nonce != data.get("nonce") or not chat.draft_at or utcnow() - chat.draft_at >= DRAFT_TTL):
        raise CommandError("這份分攤選擇已失效，請重新按「改分攤」。")
    selected = [member_id for member_id in chat.draft["members"] if member_id in active]
    if data["exp"] == "toggle":
        target = next((member_id for member_id in active if str(member_id) == data.get("who")), None)
        if target is None:
            raise CommandError("這位成員目前無法分攤。")
        if target in selected:
            selected.remove(target)
        else:
            selected.append(target)
        chat.draft = {"expense": expense.id, "members": selected}
    if data["exp"] == "splitdone":
        if not selected:
            return _split_picker(expense, roster, {"members": []}, chat.draft_nonce, page)
        shares = split.build_shares(expense.amount, expense.payer_id, (row[0] for row in roster.values()), include=selected)
        trips.replace_shares(session, expense, shares)
        trips.audit(session, trip.id, actor_id, "line", "修改分攤", f"#{expense.id} {expense.item}")
        chat.draft, chat.draft_nonce, chat.draft_at = None, None, None
        return [expense_card(session, trip, expense, roster, "已改分攤")]
    return _split_picker(expense, roster, chat.draft, chat.draft_nonce, page)


def handle(session: Session, group_id: str | None, user_id: str, data: dict) -> list[dict]:
    raw_id = data.get("id", "")
    if not (raw_id.isascii() and raw_id.isdigit() and len(raw_id) <= 18):
        raise CommandError("這個支出按鈕已失效。")
    action = data["exp"]
    if action in ("splitdone", "setpayer", "setcat", "delok", "undo"):
        # 和網頁刪除共用寫入鎖，避免寫入已移除的帳本或成員。
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    expense = session.get(Expense, int(raw_id))
    trip = session.get(Trip, expense.trip_id) if expense else None
    if trip is not None and trip.deleted_at is not None:
        raise CommandError("這個支出按鈕已失效。")
    actor = session.exec(select(Member).where(Member.line_user_id == user_id)).first()
    if not trip or (group_id and trip.line_group_id != group_id) or not actor or not session.get(TripMember, (trip.id, actor.id)):
        raise CommandError("只有這本帳的成員能編輯支出。若在帳本群組，請先傳「加入」。")
    roster = trips.people(session, trip.id)
    actor_name = trips.name_of(roster, actor.id)
    if action == "undo":
        if expense.deleted_at is None:
            return _reply("這筆支出已復原。")
        expense.deleted_at, expense.deleted_by = None, None
        trips.audit(session, trip.id, actor.id, "line", "復原支出", f"#{expense.id} {expense.item}")
        return [expense_card(session, trip, expense, roster, f"{actor_name} 已復原")]
    if expense.deleted_at is not None:
        by = f"已由{trips.name_of(roster, expense.deleted_by)}刪除" if expense.deleted_by in roster else "已刪除"
        return [line.card_message(f"「{expense.item}」{by}。", [_button("復原", "undo", expense.id)], columns=1)]
    if action == "del":
        # 卡片上的刪除按鈕會一直保留，先確認以免誤觸。
        return [line.card_message(f"確定刪除「{expense.item} {money(expense.amount, expense.currency)}」？",
                                  [_button("確定刪除", "delok", expense.id), _button("取消", "keep", expense.id)])]
    if action == "keep":
        return [expense_card(session, trip, expense, roster, "未刪除")]
    if action == "delok":
        expense.deleted_at, expense.deleted_by = utcnow(), actor.id
        trips.audit(session, trip.id, actor.id, "line", "刪除支出", f"#{expense.id} {expense.item}")
        return [deleted_expense_card(expense, actor_name)]

    if action in ("split", "toggle", "splitpage", "splitdone"):
        chat_key = f"{group_id}:{user_id}" if group_id else user_id
        return _edit_split(session, trip, expense, roster, chat_key, data, actor.id)
    if action == "payer":
        ids = list(roster)
        page = _page(data, ids)
        buttons = [_button(trips.name_of(roster, member_id), "setpayer", expense.id, who=member_id)
                   for member_id in ids[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        _navigation(buttons, "payer", expense.id, page, len(ids))
        role = "收款人" if expense.kind == "refund" else "付款人"
        return _reply(f"選擇「{expense.item}」的{role}。", buttons)
    if action == "setpayer":
        member_id = next((key for key in roster if str(key) == data.get("who")), None)
        if member_id is None:
            raise CommandError("付款人必須是帳本成員。")
        expense.payer_id = member_id
        trips.audit(session, trip.id, actor.id, "line", "修改付款人", f"#{expense.id} {expense.item} → 成員 #{member_id}")
        return [expense_card(session, trip, expense, roster, "已改收款人" if expense.kind == "refund" else "已改付款人")]
    if action == "cat":
        return _reply(f"選擇「{expense.item}」的類別。", [_button(name, "setcat", expense.id, category=index)
                                                      for index, name in enumerate(trips.CATEGORIES)])
    if action == "setcat":
        category = next((name for index, name in enumerate(trips.CATEGORIES) if str(index) == data.get("category")), None)
        if category is None:
            raise CommandError("類別不存在。")
        trips.set_category(session, expense, category)
        trips.audit(session, trip.id, actor.id, "line", "修改類別", f"#{expense.id} {expense.item} → {category}")
        return [expense_card(session, trip, expense, roster, "已改類別")]
    raise CommandError("這個按鈕已失效。")
