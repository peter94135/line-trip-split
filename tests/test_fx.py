from datetime import timedelta

import httpx

from app.db import init_db
from app.services import fx


def test_rates_reuses_daily_cache_and_falls_back_after_failed_refresh(db_engine, monkeypatch):
    init_db()
    fetched = []

    def fetch_rates():
        fetched.append(True)
        return {"JPY": 5.2}

    monkeypatch.setattr(fx, "_retry_after", 0)
    monkeypatch.setattr(fx, "_fetch", fetch_rates)
    now = fx.utcnow()
    monkeypatch.setattr(fx, "utcnow", lambda: now)
    assert fx.rates() == {"JPY": 5.2}
    assert fx.rates() == {"JPY": 5.2}
    assert len(fetched) == 1

    def unavailable():
        fetched.append(False)
        raise httpx.ConnectError("離線")

    monkeypatch.setattr(fx, "utcnow", lambda: now + timedelta(days=1))
    monkeypatch.setattr(fx, "_fetch", unavailable)
    assert fx.rates() == {"JPY": 5.2}
    assert fx.rates() == {"JPY": 5.2}
    assert fetched == [True, False]
