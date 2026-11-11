"""偵測應用程式是否還在 Tailscale sidecar 目前的網路命名空間。

應用程式以 `network_mode: service:ts` 共用 sidecar 的網路。sidecar 單獨被 Docker 重啟後會換成新的
命名空間，本容器卻留在舊的，Funnel 就一直回 502。sidecar 在共用網路的 127.0.0.1 提供健康檢查：
連線被拒代表我們已不在同一個命名空間，連續數次後結束程序，讓 Docker 依重啟政策重新啟動並加入新的網路。
sidecar 有回應但狀態不佳（例如暫時離線）時重啟本容器沒有幫助，所以不算失敗。
"""

import logging
import os
import signal
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)
FAILURES_BEFORE_EXIT = 4
_failures = 0


def _stop() -> None:
    # uvicorn 是容器的主程序，收到 SIGTERM 會正常關閉，接著由 Docker 重新啟動。
    os.kill(os.getpid(), signal.SIGTERM)


def check(url: str, *, stop: Callable[[], None] = _stop) -> None:
    global _failures
    try:
        httpx.get(url, timeout=3)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _failures += 1
        log.warning("連不到 Tailscale sidecar 的健康檢查（第 %s 次）", _failures)
        if _failures >= FAILURES_BEFORE_EXIT:
            _failures = 0
            log.error("應用程式已不在 sidecar 的網路命名空間，結束程序讓 Docker 重新啟動")
            stop()
        return
    except httpx.HTTPError:
        # 讀取逾時等情況無法判斷命名空間，不計入也不清除。
        return
    _failures = 0
