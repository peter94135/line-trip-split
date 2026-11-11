import pytest

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
