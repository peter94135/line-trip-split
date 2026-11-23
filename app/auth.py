"""將 LINE 驗證過的身分換成旅伴分帳的短期登入憑證。"""

import hmac
import time
from collections import deque
from datetime import timedelta
from threading import Lock
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.models import Member, utcnow
from app.services import admin_push

ISSUER = "trip-split"
LINE_ISSUER = "https://access.line.me"
VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
_bearer = HTTPBearer(auto_error=False)
_admin_failures: deque[float] = deque()
_admin_lock = Lock()
Database = Annotated[Session, Depends(get_session)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def verify_line_id(id_token: str) -> tuple[str, str | None]:
    channel = get_settings().line_login_channel_id
    if not channel:
        raise HTTPException(503, "LINE 登入尚未設定")
    try:
        response = httpx.post(VERIFY_URL, data={"id_token": id_token, "client_id": channel}, timeout=8)
    except httpx.HTTPError as exc:
        raise HTTPException(503, "暫時無法連線 LINE，請稍後再試") from exc
    if response.status_code in (400, 401, 403):
        raise HTTPException(401, "LINE 登入已失效，請重新登入")
    if response.status_code != 200:
        raise HTTPException(503, "暫時無法驗證 LINE 登入，請稍後再試")
    try:
        profile = response.json()
        subject = profile["sub"]
        if (profile.get("aud") != channel or profile.get("iss") != LINE_ISSUER or profile["exp"] <= time.time()
                or not isinstance(subject, str) or not subject or len(subject) > 128):
            raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(401, "LINE 登入驗證失敗") from exc
    name = profile.get("name")
    return subject, name.strip()[:100] if isinstance(name, str) and name.strip() else None


def issue_token(subject: str, role: str) -> str:
    now = utcnow()
    # 成員憑證只代表 LINE 身分，每次請求仍會重新檢查帳本權限。
    expiry = now + (timedelta(hours=1) if role == "admin" else timedelta(days=7))
    return jwt.encode({"iss": ISSUER, "aud": role, "sub": subject, "iat": now, "exp": expiry},
                      get_settings().signing_secret, algorithm="HS256")


def _subject(credentials: Credentials, role: str) -> str:
    if credentials is None:
        raise HTTPException(401, "請先登入")
    try:
        return jwt.decode(credentials.credentials, get_settings().signing_secret, algorithms=["HS256"],
                          issuer=ISSUER, audience=role, options={"require": ["sub", "exp", "iat"]})["sub"]
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "登入已過期或權限不符，請重新登入") from exc


def require_member(session: Database, credentials: Credentials) -> Member:
    subject = _subject(credentials, "member")
    member = session.get(Member, int(subject)) if subject.isascii() and subject.isdigit() and len(subject) < 19 else None
    if member is None or member.line_user_id is None:
        raise HTTPException(401, "找不到 LINE 登入身分")
    return member


def require_admin(credentials: Credentials) -> None:
    if not get_settings().admin_password or _subject(credentials, "admin") != "admin":
        raise HTTPException(401, "請先登入管理頁")


def admin_login(password: str) -> str:
    expected = get_settings().admin_password
    if not expected:
        raise HTTPException(503, "管理員密碼尚未設定")
    with _admin_lock:
        now = time.monotonic()
        while _admin_failures and now - _admin_failures[0] >= 60:
            _admin_failures.popleft()
        if len(_admin_failures) >= 8:
            raise HTTPException(429, "登入嘗試過多，請稍後再試")
        if not hmac.compare_digest(expected.encode(), password.encode()):
            _admin_failures.append(now)
            if len(_admin_failures) == 8:
                admin_push.spawn(admin_push.event, "admin_login", "管理頁登入失敗過多",
                                 "1 分鐘內密碼錯誤 8 次，已暫停登入。不是你的話，請考慮更換 ADMIN_PASSWORD。")
            raise HTTPException(401, "管理員密碼錯誤")
        _admin_failures.clear()
    return issue_token("admin", "admin")
