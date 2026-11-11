from datetime import date
from decimal import Decimal

import pytest

from app.models import Expense, ExpenseShare, Transfer, TripMember, utcnow
from app.services.split import build_shares, convert, settlement


@pytest.fixture
def members():
    return [
        TripMember(trip_id=1, member_id=1),
        TripMember(trip_id=1, member_id=2),
        TripMember(trip_id=1, member_id=3, weight=0.5),
        TripMember(trip_id=1, member_id=4, active=False),
    ]


def expense(**changes):
    data = dict(id=1, trip_id=1, spent_on=date(2026, 11, 10), item="晚餐",
                category="餐飲", category_source="user", amount=100, currency="TWD",
                rate=1, amount_settle=100, payer_id=1, source="web")
    return Expense(**(data | changes))


def transfer(**changes):
    data = dict(id=1, trip_id=1, from_id=2, to_id=1, amount=40,
                currency="TWD", rate=1, amount_settle=40)
    return Transfer(**(data | changes))


def test_default_shares_snapshot_active_member_weights(members):
    shares = build_shares(100, 1, members)
    assert [(share.member_id, share.weight) for share in shares] == [(1, 1), (2, 1), (3, 0.5)]

    result = settlement([expense()], {1: shares}, [], "TWD")
    assert result.total == 100
    assert result.paid == {1: 100}
    assert result.owed == {1: 40, 2: 40, 3: 20}
    assert [(p.from_id, p.to_id, p.amount) for p in result.payments] == [(2, 1, 40), (3, 1, 20)]


def test_select_exclude_and_treat(members):
    selected = build_shares(100, 1, members, include=[1, 3], weights={3: 1})
    assert settlement([expense()], {1: selected}, [], "TWD").owed == {1: 50, 3: 50}
    excluded = build_shares(100, 1, members, exclude=[2])
    assert {share.member_id for share in excluded} == {1, 3}
    treated = build_shares(100, 1, members, treat=True)
    assert not settlement([expense()], {1: treated}, [], "TWD").payments

    with pytest.raises(ValueError, match="停用"):
        build_shares(100, 1, members, include=[4])
    with pytest.raises(ValueError, match="帳本成員"):
        build_shares(100, 1, members, include=[99])


def test_exact_shares_validate_original_currency_total(members):
    shares = build_shares(0.3, 1, members, exact={2: 0.1, 3: 0.2})
    entry = expense(amount=0.3, currency="USD", rate=30, amount_settle=9, split_mode="exact")
    assert settlement([entry], {1: shares}, [], "TWD").owed == {2: 3, 3: 6}
    with pytest.raises(ValueError, match="分攤金額合計"):
        build_shares(0.3, 1, members, exact={2: 0.1, 3: 0.3})


def test_rate_priority_and_cross_currency():
    rates = {"JPY": 5, "USD": 0.03}
    assert convert(1000, "JPY", "TWD", fixed_rates={"JPY": 0.2135}, rates=rates) == (
        Decimal("0.2135"), Decimal("213.5000"),
    )
    assert convert(1000, "JPY", "USD", rates=rates) == (Decimal("0.006"), Decimal("6"))
    assert convert(1200, "JPY", "JPY") == (Decimal(1), Decimal(1200))
    assert convert(1000, "JPY", "USD") == (None, None)


def test_rounding_preserves_total_and_gives_remainder_to_payer(members):
    shares = build_shares(100, 1, members, weights={3: 1})
    result = settlement([expense()], {1: shares}, [], "TWD")
    assert result.owed == {1: 34, 2: 33, 3: 33}

    entry = expense(amount=1, currency="USD", amount_settle=1)
    shares = build_shares(1, 1, members, include=[2, 3], weights={3: 2})
    result = settlement([entry], {1: shares}, [], "USD")
    assert result.owed == {2: Decimal("0.33"), 3: Decimal("0.67")}
    assert sum(result.owed.values()) == result.total


def test_rounding_never_assigns_negative_shares(members):
    shares = build_shares(1, 1, members, weights={1: 0.001, 3: 1})
    result = settlement([expense(amount=1, amount_settle=1)], {1: shares}, [], "JPY")
    assert sum(result.owed.values()) == 1
    assert min(result.owed.values()) >= 0


def test_repayments_missing_rates_and_deleted_rows(members):
    shares = build_shares(100, 1, members, include=[1, 2])
    entries = [expense(), expense(id=2, amount_settle=None), expense(id=3, deleted_at=utcnow())]
    repayments = [transfer(amount=50, amount_settle=50), transfer(id=2, amount_settle=None),
                  transfer(id=3, deleted_at=utcnow())]
    result = settlement(entries, {1: shares}, repayments, "TWD")
    assert result.total == 100
    assert result.shares == {1: {1: Decimal(50), 2: Decimal(50)}}
    assert result.transfers == {1: Decimal(50)}
    assert result.balances == {1: 0, 2: 0}
    assert result.payments == []
    assert result.missing_expense_ids == [2]
    assert result.missing_transfer_ids == [2]


def test_settlement_detail_matches_paid_and_owed_after_rounding(members):
    entries = [expense(amount_settle=100.4), expense(id=2, payer_id=2, amount_settle=25.5)]
    shares = {entry.id: build_shares(entry.amount, entry.payer_id, members, include=[1, 2, 3], weights={3: 1})
              for entry in entries}
    result = settlement(entries, shares, [transfer(amount_settle=10.5)], "JPY")
    assert result.paid == {1: Decimal(100), 2: Decimal(26)}
    assert result.owed == {
        member_id: sum(row.get(member_id, 0) for row in result.shares.values())
        for member_id in (1, 2, 3)
    }
    assert result.shares[1] == {1: Decimal(34), 2: Decimal(33), 3: Decimal(33)}
    assert result.balances[2] == result.paid[2] - result.owed[2] + result.transfers[1]


def test_refund_returns_each_members_share():
    people = [TripMember(trip_id=1, member_id=key) for key in range(1, 5)]
    hotel = expense(amount=10_000, amount_settle=10_000, item="住宿")
    refund = expense(id=2, amount=2_000, amount_settle=2_000, item="住宿退款", kind="refund")
    shares = {hotel.id: build_shares(hotel.amount, 1, people), refund.id: build_shares(refund.amount, 1, people)}
    result = settlement([hotel, refund], shares, [], "TWD")
    net = expense(amount=8_000, amount_settle=8_000, item="住宿")
    expected = settlement([net], {net.id: build_shares(net.amount, 1, people)}, [], "TWD")
    assert (result.paid, result.owed, result.balances, result.payments) == (
        expected.paid, expected.owed, expected.balances, expected.payments,
    )
    assert result.total == 8_000 and result.shares[refund.id] == {key: -500 for key in range(1, 5)}


def test_guarantees_fewest_transfers_when_largest_first_is_suboptimal():
    entries = [expense(id=1, payer_id=1, amount=10, amount_settle=10, split_mode="exact"),
               expense(id=2, payer_id=2, amount=8, amount_settle=8, split_mode="exact")]
    shares = {
        1: [ExpenseShare(expense_id=1, member_id=3, amount=6), ExpenseShare(expense_id=1, member_id=4, amount=4)],
        2: [ExpenseShare(expense_id=2, member_id=5, amount=8)],
    }
    # 淨額為 +10、+8、-6、-4、-8。三個欠款人至少需要三筆。
    result = settlement(entries, shares, [], "TWD")
    assert len(result.payments) == 3
    assert {(p.from_id, p.to_id, p.amount) for p in result.payments} == {
        (3, 1, 6), (4, 1, 4), (5, 2, 8),
    }


def test_guarantees_fewest_transfers_without_equal_amount_pairs():
    entries = [expense(id=1, payer_id=1, amount=4, amount_settle=4, split_mode="exact"),
               expense(id=2, payer_id=2, amount=6, amount_settle=6, split_mode="exact")]
    shares = {
        1: [ExpenseShare(expense_id=1, member_id=3, amount=2), ExpenseShare(expense_id=1, member_id=4, amount=2)],
        2: [ExpenseShare(expense_id=2, member_id=5, amount=3), ExpenseShare(expense_id=2, member_id=6, amount=3)],
    }
    # 最大金額優先會用五筆。把兩筆 2 元和兩筆 3 元分開結清只要四筆。
    result = settlement(entries, shares, [], "TWD")
    assert {(p.from_id, p.to_id, p.amount) for p in result.payments} == {
        (3, 1, 2), (4, 1, 2), (5, 2, 3), (6, 2, 3),
    }
