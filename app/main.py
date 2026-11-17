import json
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, get_settings
from app.db import init_db
from app.models import utcnow
from app.routers import line_webhook, web
from app.services import line, line_delivery, trips, watchdog


def _app_logging() -> None:
    # uvicorn 只輸出自己的紀錄；刪除、復原等操作紀錄要另外接到標準輸出，才查得到是誰做的。
    logger = logging.getLogger("app")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_app_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    line_webhook.resume_inbox()
    scheduler = BackgroundScheduler(timezone="UTC")
    if line.is_configured():
        scheduler.add_job(line_webhook.drain_inbox, "interval", seconds=30, next_run_time=utcnow())
        scheduler.add_job(line_delivery.retry_pending, "interval", seconds=30, next_run_time=utcnow())
        scheduler.add_job(trips.fill_missing_rates, "interval", minutes=5)
    if health_url := get_settings().ts_health_url:
        scheduler.add_job(watchdog.check, "interval", seconds=15, args=[health_url])
    if scheduler.get_jobs():
        scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(title="旅伴分帳", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.include_router(line_webhook.router)
app.include_router(web.router)
WEB_DIR = BASE_DIR / "web"
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/") or request.url.path in ("/liff/", "/admin", "/liff/sw.js"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    elif request.url.path.startswith("/assets/") and "v" in request.query_params and response.status_code == 200:
        # 網址帶版本號的檔案內容不會變，LINE 內建瀏覽器重開時不必再下載或確認。
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    detail = first.get("ctx", {}).get("error") if first.get("type") == "value_error" else None
    return JSONResponse({"detail": str(detail) if detail else "請確認必填欄位與輸入格式"}, status_code=422)


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/liff/")


@app.get("/liff/", include_in_schema=False)
def liff_page() -> HTMLResponse:
    # 設定直接放進頁面，打開時少等一次 /api/liff/config。
    config = json.dumps(web.liff_config(), ensure_ascii=False).replace("<", "\\u003c")
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("</head>", f'<script id="liff-config" type="application/json">{config}</script>\n</head>', 1))


@app.get("/liff/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}
