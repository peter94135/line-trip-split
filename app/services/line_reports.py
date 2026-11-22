"""LINE 帳本查詢、統計及結算回覆。"""

from urllib.parse import quote

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Expense, Trip
from app.services import line, line_settlement, trip_stats, trips
from app.services.line_parse import CommandError, money


def reply(text: str, buttons: list[line.Button] | None = None) -> list[dict]:
    return [line.text_message(text, buttons)]


def group_invite(trip: Trip) -> str:
    settings = get_settings()
    if not settings.liff_id or not settings.line_login_channel_id:
        return ""
    return f"\n開啟帳本，登入後即加入：\n{trips.invite_url(trip, settings.liff_id)}"


def _trip_header(trip: Trip) -> str:
    dates = " ～ ".join(day.isoformat() for day in (trip.start_date, trip.end_date) if day)
    return f"{trip.name}{f'｜{dates}' if dates else ''}｜{'進行中' if trip.status == 'open' else '已結束'}"


def trip_link(trip: Trip, *, tab: str = "", label: str = "") -> str:
    liff_id = get_settings().liff_id
    if not liff_id:
        return ""
    url = f"https://liff.line.me/{quote(liff_id, safe='')}?trip={trip.id}"
    return "\n" + label + url + (f"&tab={tab}" if tab else "")


def _personal_settlement(session: Session, trip: Trip, speaker: int) -> list[dict]:
    result = trips.total(session, trip)
    roster = trips.people(session, trip.id)
    label = lambda member_id: "我" if member_id == speaker else trips.name_of(roster, member_id)
    fmt = lambda amount: money(amount, trip.settle_currency)
    lines = []
    missing = [f"支出 #{key}" for key in result.missing_expense_ids] + [f"還款 #{key}" for key in result.missing_transfer_ids]
    if missing:
        lines.append("⚠️ 以下紀錄待換算，暫未計入結算：" + "、".join(missing))
    lines.append(f"{_trip_header(trip)}｜共 {fmt(result.total)}")
    lines.append(f"我　付 {fmt(result.paid.get(speaker, 0))}｜分擔 {fmt(result.owed.get(speaker, 0))}")
    payments = [f"{label(payment.from_id)} → {label(payment.to_id)}　{fmt(payment.amount)}"
                for payment in result.payments if speaker in (payment.from_id, payment.to_id)]
    lines.extend(["結算", *(payments or ["目前無須轉帳"])])
    return reply("\n".join(lines) + trip_link(trip, tab="settle", label="結算明細："))


def statistics_reply(session: Session, trip: Trip, speaker: int, *, personal: bool) -> list[dict]:
    stats = trip_stats.calculate(session, trip)
    currency = stats["currency"]
    if personal:
        total = stats["by_member"][speaker]["owed"]
        lines = [f"{trip.name}｜我的分擔 {money(total, currency)}"]
        for row in stats["by_category"]:
            category = row["category"]
            amounts = stats["by_member_category"][category].get(speaker)
            if amounts and amounts["owed"]:
                lines.append(f'{category}　{money(amounts["owed"], currency)}')
    else:
        lines = [f'{trip.name}｜總花費 {money(stats["total"], currency)}']
        for row in stats["by_category"][:5]:
            lines.append(f'{row["category"]}　{money(row["amount"], currency)}（{row["percent"]:g}%）')
    if stats["missing_count"]:
        lines.append(f'{stats["missing_count"]} 筆缺匯率未計入')
    return reply("\n".join(lines) + trip_link(trip, tab="stats", label="統計："))


def _details(session: Session, trip: Trip) -> list[dict]:
    roster = trips.people(session, trip.id)
    rows = session.exec(select(Expense).where(Expense.trip_id == trip.id, Expense.deleted_at.is_(None))
                        .order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(15)).all()
    lines = [f"{trip.name}｜最近 15 筆"]
    for row in rows:
        kind_label, verb = ("退款｜", "收") if row.kind == "refund" else ("", "付")
        lines.append(f"{row.spent_on:%m/%d} {kind_label}{row.item}　{money(row.amount, row.currency)}｜{trips.name_of(roster, row.payer_id)}{verb}")
    if not rows:
        lines.append("目前沒有支出。")
    return reply("\n".join(lines) + trip_link(trip))


def read_command(session: Session, trip: Trip, speaker: int, user_id: str, text: str) -> list[dict]:
    if text == "帳本":
        link = group_invite(trip) if trip.line_group_id and trip.status == "open" else ""
        roster = trips.people(session, trip.id)
        payer = f"\n預設付款人 {trips.name_of(roster, trip.default_payer_id)}" if trip.default_payer_id in roster else ""
        return reply(f"{_trip_header(trip)}\n記帳幣別 {trip.default_currency}｜結算幣別 {trip.settle_currency}{payer}"
                     + (link or trip_link(trip)))
    if text in ("加入", "成員"):
        roster = trips.people(session, trip.id)
        if text == "加入":
            status = "已加入" if roster[speaker][0].active else "已在帳本內，目前分攤已停用"
            return reply(f"{status}「{trip.name}」。")
        labels = [trips.name_of(roster, key) + ("（已停用）" if not row[0].active else "") for key, row in roster.items()]
        return reply(f"{trip.name}｜成員 {len(roster)} 人\n" + "\n".join(labels))
    if text == "明細":
        return _details(session, trip)
    if text == "結算":
        return [line_settlement.card(session, trip)]
    if text == "結束帳本":
        if trip.status == "closed":
            return reply(f"「{trip.name}」已結束。")
        if trip.created_by != speaker and get_settings().line_admin_user_id != user_id:
            raise CommandError("請由帳本建立者或管理員結束帳本。")
        trip.status = "closed"
        trips.audit(session, trip.id, speaker, "line", "結束帳本")
        return [line_settlement.card(session, trip, closing="已結束")]
    return _personal_settlement(session, trip, speaker)
