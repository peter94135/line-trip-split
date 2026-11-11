"""每日快取以 TWD 為基準的牌告匯率。"""

import json
import logging
import time
from threading import Lock
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine
from app.models import Setting, utcnow

log = logging.getLogger(__name__)

URL = "https://open.er-api.com/v6/latest/TWD"
ATTRIBUTION = "Rates By Exchange Rate API"
ATTRIBUTION_URL = "https://www.exchangerate-api.com"
SETTING = "fx_rates"
RETRY_S = 600
_lock = Lock()
_retry_after = 0.0


def _fetch() -> dict[str, float]:
    response = httpx.get(URL, timeout=8)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("result") != "success" or not isinstance(data.get("rates"), dict):
        raise ValueError("匯率查詢失敗")
    return data["rates"]


def rates() -> dict[str, float]:
    """回傳 1 台幣可換的外幣。查詢失敗時使用上次成功的匯率。"""
    global _retry_after
    with _lock, Session(get_engine()) as session:
        today = utcnow().astimezone(ZoneInfo(get_settings().tz)).date().isoformat()
        row = session.get(Setting, SETTING)
        cached = json.loads(row.value) if row else {}
        if cached.get("date") == today or time.monotonic() < _retry_after:
            return cached.get("rates", {})

        try:
            fresh = _fetch()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("查不到匯率：%s", exc)
            _retry_after = time.monotonic() + RETRY_S
            return cached.get("rates", {})

        session.merge(Setting(key=SETTING, value=json.dumps({"date": today, "rates": fresh})))
        session.commit()
        _retry_after = 0
        return fresh
