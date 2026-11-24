import json
import time
from datetime import date
from types import SimpleNamespace

import pytest
from google.genai import errors
from sqlmodel import Session, select

from app.db import init_db
from app.models import GeminiCall, Setting
from app.services import gemini, trips


def test_model_fallback_cooldown_and_usage(db_engine, monkeypatch):
    init_db()
    primary, fallback = "gemini-3.5-flash", "gemini-3.1-flash-lite"
    monkeypatch.setattr(gemini, "_models", lambda: [primary, fallback])
    calls = []

    def generate_content(*, model, contents, config):
        calls.append((model, bool(config.thinking_config)))
        assert config.response_schema is gemini.TripParse
        assert "、".join(trips.CATEGORIES) in config.system_instruction
        assert json.loads(contents[0])["default_currency"] == "JPY"
        if model == primary:
            raise errors.ClientError(429, {"error": {"message": "quota", "details": [{"retryDelay": "90s"}]}})
        if config.thinking_config:
            raise errors.ClientError(400, {"error": {"message": "Thinking level not supported"}})
        return SimpleNamespace(
            parsed=None, text='{"task_type":"expense_add","expenses":[{"item":"咖啡","amount":120}]}',
            usage_metadata=SimpleNamespace(prompt_token_count=1000, cached_content_token_count=200,
                                           candidates_token_count=100, thoughts_token_count=50),
        )

    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    context = dict(today=date(2026, 11, 10), currency="JPY", members=[], mentions={})
    assert gemini.parse("咖啡 120", **context).expenses[0].amount == 120
    assert calls == [(primary, True), (fallback, True), (fallback, False)]
    assert gemini.parse("咖啡 120", **context).expenses[0].item == "咖啡"
    assert calls[3:] == [(fallback, True), (fallback, False)]
    with Session(db_engine) as session:
        cooldown = json.loads(session.get(Setting, f"gemini_model:{primary}").value)
        assert cooldown["calls"] == 1 and cooldown["status"] == 429
        usage = session.exec(select(GeminiCall)).all()
        assert len(usage) == 2 and all(call.model == fallback for call in usage)
        assert (usage[0].input_tokens, usage[0].cached_tokens, usage[0].output_tokens, usage[0].thought_tokens) == (1000, 200, 150, 50)
        assert usage[0].cost_usd > 0


def test_missing_model_falls_back_and_is_skipped(db_engine, monkeypatch):
    init_db()
    gone, fallback = "gemini-gone", "gemini-3.1-flash-lite"
    models = [gone, fallback]
    monkeypatch.setattr(gemini, "_models", lambda: models)
    calls = []

    def generate_content(*, model, contents, config):
        calls.append(model)
        if model == gone:
            raise errors.ClientError(404, {"error": {"message": f"models/{gone} is not found for API version v1"}})
        return SimpleNamespace(parsed=None, text='{"task_type":"expense_add","expenses":[{"item":"咖啡","amount":120}]}',
                               usage_metadata=None)

    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    context = dict(today=date(2026, 11, 10), currency="JPY", members=[], mentions={})
    assert gemini.parse("咖啡 120", **context).expenses[0].amount == 120
    assert gemini.parse("咖啡 120", **context).expenses[0].amount == 120
    # 404 只試一次就換下一個模型，之後先跳過，不再浪費一次請求。
    assert calls == [gone, fallback, fallback]
    with Session(db_engine) as session:
        stat = json.loads(session.get(Setting, f"gemini_model:{gone}").value)
        assert stat["status"] == 404 and stat["retry_at"] > time.time() + 3600

    models[:] = ["gemini-also-gone"]
    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: (_ for _ in ()).throw(errors.ClientError(404, {"error": {"message": "not found"}})))))
    with pytest.raises(gemini.GeminiError, match="模型都找不到"):
        gemini.parse("咖啡 120", **context)


def test_model_labels_match_ai_studio():
    assert gemini.label("gemini-3.5-flash") == "Gemini 3.6 Flash"
    assert gemini.label("gemini-3.5-flash-lite") == "Gemini 3.5 Flash Lite"
    assert gemini.label("gemini-3.1-flash-lite") == "Gemini 3.1 Flash Lite"
    assert gemini.label("custom-model") == "custom-model"
