"""以結算已捨入的分攤金額統計旅程花費。"""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from sqlmodel import Session

from app.models import Trip
from app.services import split, trips


def _average(total: Decimal, count: Decimal | int) -> Decimal:
    return (total / count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if count else Decimal(0)


def calculate(session: Session, trip: Trip) -> dict:
    roster = trips.people(session, trip.id)
    entries = trips.expenses(session, trip.id)
    result = split.settlement(entries, trips.shares_by_expense(session, trip.id), (), trip.settle_currency)
    by_category = defaultdict(Decimal)
    by_day = defaultdict(Decimal)
    by_member_category = defaultdict(dict)

    def member_category(category: str, member_id: int) -> dict[str, Decimal]:
        return by_member_category[category].setdefault(member_id, {"paid": Decimal(0), "owed": Decimal(0)})

    for entry in entries:
        if entry.id not in result.shares:
            continue
        shares = result.shares[entry.id]
        amount = sum(shares.values(), Decimal(0))
        by_category[entry.category] += amount
        by_day[entry.spent_on] += amount
        member_category(entry.category, entry.payer_id)["paid"] += amount
        for member_id, share in shares.items():
            member_category(entry.category, member_id)["owed"] += share

    start = trip.start_date or min(by_day, default=None)
    end = trip.end_date or max(by_day, default=None)
    days = max((end - start).days + 1, 0) if start and end else 0
    return {
        "currency": trip.settle_currency,
        "total": result.total,
        "days": days,
        "average_per_day": _average(result.total, days),
        "average_per_member": _average(result.total, len(roster)),
        "by_category": [
            {"category": category, "amount": amount,
             "percent": _average(amount * 100, result.total)}
            for category, amount in sorted(by_category.items(), key=lambda row: (-row[1], row[0]))
        ],
        "by_day": [{"day": day, "amount": amount} for day, amount in sorted(by_day.items())],
        "by_member": {
            member_id: {"paid": result.paid.get(member_id, 0), "owed": result.owed.get(member_id, 0)}
            for member_id in roster
        },
        "by_member_category": dict(by_member_category),
        "missing_count": len(result.missing_expense_ids),
    }
