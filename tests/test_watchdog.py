import httpx
import pytest

from app.services import watchdog


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(watchdog, "_failures", 0)
    results = []

    def get(url, *, timeout):
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    monkeypatch.setattr(watchdog.httpx, "get", get)
    stops = []

    def run(*outcomes):
        results.extend(outcomes)
        for _ in outcomes:
            watchdog.check("http://127.0.0.1:9002/healthz", stop=lambda: stops.append(True))
        return len(stops)

    return run


def refused():
    return httpx.ConnectError("Connection refused")


def test_stale_namespace_stops_after_consecutive_refusals(probe):
    # 留在舊命名空間時，sidecar 的健康檢查一定連線被拒。
    assert probe(refused(), refused(), refused()) == 0
    assert probe(refused()) == 1


def test_sidecar_reply_resets_count_even_when_unhealthy(probe):
    # sidecar 有回應就表示還在同一個命名空間；暫時離線回 503 時重啟本容器沒有幫助。
    assert probe(refused(), refused(), refused(), 503, refused(), refused(), refused()) == 0
    assert probe(httpx.ReadTimeout("slow"), refused()) == 1
