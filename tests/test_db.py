from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.db import init_db
from app.models import Member, Trip


def test_one_open_trip_per_line_group(db_engine):
    init_db()
    with Session(db_engine) as session:
        old = Trip(name="京都", line_group_id="group")
        session.add(old)
        session.commit()

        session.add(Trip(name="大阪", line_group_id="group"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        old.status = "closed"
        session.commit()
        new = Trip(name="大阪", line_group_id="group")
        session.add(new)
        session.commit()
        assert old.invite_code != new.invite_code
        assert len(new.invite_code) == 8


def test_member_timestamp_round_trips_as_aware_utc(db_engine):
    init_db()
    taipei_time = datetime(2026, 11, 10, 12, tzinfo=timezone(timedelta(hours=8)))
    with Session(db_engine) as session:
        member = Member(line_name="小明", created_at=taipei_time)
        session.add(member)
        session.commit()
        session.refresh(member)
        assert member.created_at == datetime(2026, 11, 10, 4, tzinfo=UTC)
        assert member.created_at.tzinfo is UTC


def test_existing_trip_member_gets_optional_payment_note(db_engine):
    init_db()
    with db_engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO member (id, created_at) VALUES (1, '2026-11-10')")
        connection.exec_driver_sql("""
            INSERT INTO trip (id, name, status, timezone, default_currency, settle_currency, invite_code, created_at, ledger_version)
            VALUES (1, '京都', 'open', 'Asia/Taipei', 'TWD', 'TWD', 'Old12345', '2026-11-10', 0)
        """)
        connection.exec_driver_sql("""
            INSERT INTO tripmember (trip_id, member_id, weight, active, joined_at) VALUES (1, 1, 1, 1, '2026-11-10')
        """)
        connection.exec_driver_sql("ALTER TABLE tripmember DROP COLUMN payment_note")
    init_db()
    with db_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT member_id, payment_note FROM tripmember").one() == (1, None)


def test_existing_expense_defaults_to_expense_and_enforces_kind(db_engine):
    init_db()
    with db_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE expense DROP COLUMN kind")
        connection.exec_driver_sql("INSERT INTO member (id, created_at) VALUES (1, '2026-11-10')")
        connection.exec_driver_sql("""
            INSERT INTO trip (id, name, status, timezone, default_currency, settle_currency, invite_code, created_at, ledger_version)
            VALUES (1, '京都', 'open', 'Asia/Taipei', 'TWD', 'TWD', 'Old12345', '2026-11-10', 0)
        """)
        connection.exec_driver_sql("""
            INSERT INTO tripmember (trip_id, member_id, weight, active, joined_at) VALUES (1, 1, 1, 1, '2026-11-10')
        """)
        connection.exec_driver_sql("""
            INSERT INTO expense (trip_id, payer_id, spent_on, item, category, category_source, amount,
                                 currency, split_mode, source, created_at)
            VALUES (1, 1, '2026-11-10', '晚餐', '餐飲', 'user', 100, 'TWD', 'equal', 'web', '2026-11-10')
        """)
    init_db()
    with db_engine.begin() as connection:
        assert connection.exec_driver_sql("SELECT item, kind FROM expense").one() == ("晚餐", "expense")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE expense SET kind = 'income'")
