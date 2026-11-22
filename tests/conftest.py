import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import auth
from app.main import app
from app.models import Member, Trip, TripMember
from app.services import line

from app.config import get_settings
from app.db import get_engine


@pytest.fixture
def db_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine = get_engine()
    try:
        yield engine
    finally:
        engine.dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clear_profile_cache():
    from app.services import line_chat
    line_chat._profile_cache.clear()
    yield
    line_chat._profile_cache.clear()


@pytest.fixture
def web(db_engine, monkeypatch):
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "test-login-channel")
    monkeypatch.setenv("LIFF_ID", "test-liff")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    get_settings.cache_clear()

    def verify(url, *, data, timeout):
        assert url == auth.VERIFY_URL and data["client_id"] == "test-login-channel" and timeout > 0
        name = data["id_token"]
        if name == "invalid":
            return httpx.Response(400)
        return httpx.Response(200, json={"sub": f"U-{name}", "name": name, "iss": auth.LINE_ISSUER,
                                         "aud": "wrong" if name == "wrong-audience" else data["client_id"],
                                         "exp": time.time() + 1000})

    monkeypatch.setattr(auth.httpx, "post", verify)
    monkeypatch.setattr(line, "get_profile", lambda user, group: {"displayName": user.removeprefix("U-")})
    with TestClient(app) as client:
        with Session(db_engine) as session:
            alice = Member(line_user_id="U-Alice", line_name="Alice")
            bob = Member(line_user_id="U-Bob", line_name="Bob")
            session.add_all([alice, bob]); session.flush()
            trip = Trip(name="京都", created_by=alice.id, line_group_id="C-test", invite_code="AbCd1234", fixed_rates={"JPY": .2})
            session.add(trip); session.flush()
            session.add_all([TripMember(trip_id=trip.id, member_id=person.id) for person in (alice, bob)])
            ids = (trip.id, alice.id, bob.id)
            session.commit()

        def login(name):
            response = client.post("/api/auth/line", json={"id_token": name})
            assert response.status_code == 200
            return {"Authorization": "Bearer " + response.json()["token"]}

        yield client, login, ids
