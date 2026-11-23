"""Application settings, loaded from backend/.env (never commit that file)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

try:   # modules that read os.environ directly (SMTP_*, NOTIFY_*_EMAIL, PUBLIC_BASE_URL) must see backend/.env too
    from dotenv import load_dotenv
    load_dotenv(BACKEND_DIR / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _sqlalchemy_url(url: str) -> str:
    """Neon gives 'postgresql://...'; SQLAlchemy needs the psycopg (v3) driver prefix."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    # Domo AI Services (Text Generation)
    domo_subdomain: str = "gwcteq-partner"
    domo_token: str = ""
    domo_model: str | None = None
    domo_json_mode: bool = False          # send responseFormat (JSON schema) to Domo
    domo_temperature: float = 0.0
    domo_max_tokens: int = 3000

    # Neon PostgreSQL
    database_url: str
    readonly_database_url: str | None = None   # optional dedicated read-only role for the SQL tool

    # App
    data_dir: str = str(BACKEND_DIR / "data" / "data")
    data_schema: str = "sp"
    cors_origins: str = "http://localhost:5173"
    agent_max_steps: int = 6

    @property
    def db_url(self) -> str:
        return _sqlalchemy_url(self.database_url)

    @property
    def ro_db_url(self) -> str:
        return _sqlalchemy_url(self.readonly_database_url or self.database_url)

    @property
    def llm_configured(self) -> bool:
        return bool(self.domo_token and self.domo_token != "your_developer_token")


settings = Settings()
