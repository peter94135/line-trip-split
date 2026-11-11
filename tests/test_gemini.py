import json
from datetime import date
from types import SimpleNamespace

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
