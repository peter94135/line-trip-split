import os
import secrets
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    data_dir: Path = BASE_DIR / "data"
    tz: str = "Asia/Taipei"
    admin_password: str = ""
    app_secret: str = ""
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    line_admin_user_id: str = ""
    line_login_channel_id: str = ""
    liff_id: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_thinking_level: str = "minimal"
    gemini_timeout_s: int = Field(default=25, gt=0)
    gemini_fallback_models: str = "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    public_base_url: str = ""
    trip_summary_push: bool = False
    ts_health_url: str = ""  # Compose 內設定；留空則不監看 sidecar
    app_version: str = ""  # 部署時由 GIT_COMMIT 建置參數帶入

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trip.db"

    @property
    def signing_secret(self) -> str:
        if self.app_secret:
            return self.app_secret
        path = self.data_dir / ".app_secret"
        if not path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", dir=self.data_dir, delete=False) as file:
                temporary = Path(file.name)
                try:
                    file.write(secrets.token_urlsafe(48))
                    file.flush()
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                finally:
                    temporary.unlink()
        return path.read_text().strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
