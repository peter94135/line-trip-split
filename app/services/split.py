"""建立分攤快照、換算匯率，以及由支出與還款計算結算。"""

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import cache

from app.models import Expense, Share, Transfer, TripMember

Number = int | float | Decimal
ZERO_DECIMAL_CURRENCIES = {"TWD", "JPY", "KRW", "VND"}
HALF = Fraction(1, 2)


def _positive(value: Number, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label}必須為正數") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label}必須為正數")
    return number


def build_shares(
    amount: Number,
    payer_id: int,
    members: Iterable[TripMember],
    *,
    include: Iterable[int] | None = None,
    exclude: Iterable[int] = (),
    weights: Mapping[int, Number] | None = None,
    exact: Mapping[int, Number] | None = None,
    treat: bool = False,
) -> list[Share]:
    """將目前的成員設定保存為這筆支出獨立的分攤快照。"""
    total = _positive(amount, "支出金額")
    by_id = {member.member_id: member for member in members}
    excluded = set(exclude)
    if payer_id not in by_id or excluded.difference(by_id):
        raise ValueError("付款人或排除對象必須是帳本成員")
    if treat or exact is not None:
        if include is not None or excluded or weights or (treat and exact is not None):
            raise ValueError("請客、指定金額與加權分攤只能選擇一種")
        if treat:
            return [Share(member_id=payer_id, weight=1)]

    if exact is not None:
        selected = set(exact)
    elif include is not None:
        selected = set(include)
    else:
        selected = {member_id for member_id, member in by_id.items() if member.active}

    if selected.difference(by_id):
        raise ValueError("分攤對象必須是帳本成員")
    selected -= excluded
    if not selected:
        raise ValueError("至少需要一位分攤成員")
    if any(not by_id[member_id].active for member_id in selected):
        raise ValueError("分攤對象包含已停用的成員")

    if exact is not None:
        amounts = {member_id: _positive(value, "分攤金額") for member_id, value in exact.items()}
        if sum(Fraction(value) for value in amounts.values()) != total:
            raise ValueError("分攤金額合計必須等於支出金額")
        return [Share(member_id=member_id, amount=amounts[member_id]) for member_id in sorted(selected)]

    weights = weights or {}
    if set(weights).difference(selected):
        raise ValueError("權重只能指定給分攤成員")
    return [
        Share(member_id=member_id, weight=_positive(weights.get(member_id, by_id[member_id].weight), "分攤權重"))
        for member_id in sorted(selected)
    ]


def convert(
    amount: Number,
    currency: str,
    settle_currency: str,
    *,
    fixed_rates: Mapping[str, Number] | None = None,
    rates: Mapping[str, Number] | None = None,
) -> tuple[Decimal | None, Decimal | None]:
    """回傳原幣到結算幣的鎖定匯率與換算金額。即時匯率表以 TWD 為基準。"""
    amount = _positive(amount, "金額")
    currency, settle_currency = currency.upper(), settle_currency.upper()
    if currency == settle_currency:
        return Decimal(1), amount
    if fixed_rates is not None and currency in fixed_rates:
        rate = _positive(fixed_rates[currency], "固定匯率")
        return rate, amount * rate

    rates = rates or {}
    source_rate = 1 if currency == "TWD" else rates.get(currency)
    target_rate = 1 if settle_currency == "TWD" else rates.get(settle_currency)
    if source_rate is None or target_rate is None:
        return None, None
    try:
        rate = _positive(target_rate, "匯率") / _positive(source_rate, "匯率")
    except ValueError:
        return None, None
    return rate, amount * rate


@dataclass(frozen=True)
class Payment:
    from_id: int
    to_id: int
    amount: Decimal


@dataclass(frozen=True)
class Settlement:
    total: Decimal
    paid: dict[int, Decimal]
    owed: dict[int, Decimal]
    balances: dict[int, Decimal]
    payments: list[Payment]
    missing_expense_ids: list[int]
    missing_transfer_ids: list[int]
    shares: dict[int, dict[int, Decimal]]
    transfers: dict[int, Decimal]


def _minor_units(amount: float, scale: int) -> Fraction:
    return Fraction(Decimal(str(amount))) * scale


def _round_shares(expense: Expense, shares: Sequence[Share], units: Fraction, total: int) -> dict[int, int]:
    parts = {}
    for share in shares:
        value = share.amount if expense.split_mode == "exact" else share.weight
        if value is None:
            raise ValueError("支出缺少分攤金額或權重")
        parts[share.member_id] = Fraction(_positive(value, "分攤值"))
    if not parts or len(parts) != len(shares):
        raise ValueError("支出缺少分攤成員或分攤成員重複")

    denominator = sum(parts.values())
    quotas = {member_id: units * value / denominator for member_id, value in parts.items()}
    rounded = {member_id: int(quota + HALF) for member_id, quota in quotas.items()}
    remainder = total - sum(rounded.values())
    payer_id = expense.payer_id
    if payer_id in rounded:
        correction = max(remainder, -rounded[payer_id])
        rounded[payer_id] += correction
        remainder -= correction
    if remainder:
        # 付款人未分攤或分攤已降到零時，優先修正偏差最大的其他分攤。
        step = 1 if remainder > 0 else -1
        candidates = sorted(rounded, key=lambda member_id: (
            step * (rounded[member_id] - quotas[member_id]), member_id,
        ))
        for member_id in candidates:
            if step < 0 and rounded[member_id] == 0:
                continue
            rounded[member_id] += step
            remainder -= step
            if remainder == 0:
                break
    return rounded


def _zero_sum_groups(nets: tuple[int, ...]) -> tuple[int, ...]:
    """找出最多個可各自結清的群組，每個群組用成員的 bit mask 表示。"""
    positive_members = sum(1 << index for index, net in enumerate(nets) if net > 0)

    def candidates(available: int, needed: int, chosen: int, credit: int, debt: int):
        if needed == 0:
            # 若再加入其他成員，新增的成員必須自己就能結清，保留給下一組處理。
            yield chosen
            return
        if needed > credit or needed < -debt:
            return

        bit = available & -available
        net = nets[bit.bit_length() - 1]
        rest = available ^ bit
        credit -= max(net, 0)
        debt -= max(-net, 0)
        yield from candidates(rest, needed - net, chosen | bit, credit, debt)
        yield from candidates(rest, needed, chosen, credit, debt)

    @cache
    def solve(mask: int) -> tuple[int, ...]:
        if not mask:
            return ()
        positives = mask & positive_members
        size = mask.bit_count()
        # 同額配對已處理，剩下每組至少三人，且至少一位債權人與一位欠款人。
        upper_bound = min(positives.bit_count(), (mask ^ positives).bit_count(), size // 3)
        best = (mask,)
        if upper_bound <= 1:
            return best

        pivot = mask & -mask
        net = nets[pivot.bit_length() - 1]
        total = sum(value for index, value in enumerate(nets) if positives & (1 << index))
        for group in candidates(mask ^ pivot, -net, pivot, total - max(net, 0), total - max(-net, 0)):
            if group == mask:
                continue
            following = solve(mask ^ group)
            if 1 + len(following) > len(best):
                best = (group,) + following
                if len(best) == upper_bound:
                    break
        return best

    return solve((1 << len(nets)) - 1)


def _payments(balances: Mapping[int, int], scale: int) -> list[Payment]:
    remaining = {member_id: net for member_id, net in sorted(balances.items()) if net}
    payments = []
    credits_by_amount = defaultdict(deque)
    for member_id, net in remaining.items():
        if net > 0:
            credits_by_amount[net].append(member_id)

    # 若同額兩人在不同的零和群組，可以配成一組，原來的其餘成員合成另一組。
    for from_id, net in tuple(remaining.items()):
        if net < 0 and credits_by_amount[-net]:
            to_id = credits_by_amount[-net].popleft()
            del remaining[from_id], remaining[to_id]
            payments.append(Payment(from_id, to_id, Decimal(-net) / scale))

    members = sorted(remaining, key=lambda member_id: (-abs(remaining[member_id]), member_id))
    nets = tuple(remaining[member_id] for member_id in members)
    # 每個轉帳圖的連通分量都是零和群組，k 人至少需 k-1 筆。組數最多時總筆數最少。
    for mask in _zero_sum_groups(nets):
        group = [member_id for index, member_id in enumerate(members) if mask & (1 << index)]
        debts = deque((member_id, -remaining[member_id]) for member_id in group if remaining[member_id] < 0)
        credits = deque((member_id, remaining[member_id]) for member_id in group if remaining[member_id] > 0)
        while debts:
            from_id, debt = debts[0]
            to_id, credit = credits[0]
            amount = min(debt, credit)
            payments.append(Payment(from_id, to_id, Decimal(amount) / scale))
            if debt == amount:
                debts.popleft()
            else:
                debts[0] = (from_id, debt - amount)
            if credit == amount:
                credits.popleft()
            else:
                credits[0] = (to_id, credit - amount)
    return payments


def settlement(
    expenses: Iterable[Expense],
    shares_by_expense: Mapping[int, Sequence[Share]],
    transfers: Iterable[Transfer],
    currency: str,
) -> Settlement:
    """用同一本帳本已鎖定的換算金額結算，忽略軟刪除與缺少匯率的紀錄。"""
    scale = 1 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 100
    paid: dict[int, int] = defaultdict(int)
    owed: dict[int, int] = defaultdict(int)
    balances: dict[int, int] = defaultdict(int)
    missing_expense_ids = []
    missing_transfer_ids = []
    rounded_shares = {}
    rounded_transfers = {}
    for expense in expenses:
        if expense.deleted_at is not None:
            continue
        if expense.id is None:
            raise ValueError("支出尚未存檔")
        if expense.amount_settle is None:
            missing_expense_ids.append(expense.id)
            continue
        units = _minor_units(expense.amount_settle, scale)
        total = int(units + HALF)
        sign = -1 if expense.kind == "refund" else 1
        paid[expense.payer_id] += sign * total
        balances[expense.payer_id] += sign * total
        amounts = _round_shares(expense, shares_by_expense.get(expense.id, ()), units, total)
        rounded_shares[expense.id] = {member_id: Decimal(sign * value) / scale for member_id, value in amounts.items()}
        for member_id, amount in amounts.items():
            owed[member_id] += sign * amount
            balances[member_id] -= sign * amount

    for transfer in transfers:
        if transfer.deleted_at is not None:
            continue
        if transfer.id is None:
            raise ValueError("還款尚未存檔")
        if transfer.amount_settle is None:
            missing_transfer_ids.append(transfer.id)
            continue
        amount = int(_minor_units(transfer.amount_settle, scale) + HALF)
        rounded_transfers[transfer.id] = Decimal(amount) / scale
        balances[transfer.from_id] += amount
        balances[transfer.to_id] -= amount

    return Settlement(
        total=Decimal(sum(paid.values())) / scale,
        paid={member_id: Decimal(value) / scale for member_id, value in paid.items()},
        owed={member_id: Decimal(value) / scale for member_id, value in owed.items()},
        balances={member_id: Decimal(value) / scale for member_id, value in balances.items()},
        payments=_payments(balances, scale),
        missing_expense_ids=missing_expense_ids,
        missing_transfer_ids=missing_transfer_ids,
        shares=rounded_shares,
        transfers=rounded_transfers,
    )
