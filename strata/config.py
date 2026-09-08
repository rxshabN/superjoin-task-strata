import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    provider: str
    gemini_api_key: str | None
    gemini_model: str
    ollama_url: str
    ollama_model: str
    pages_per_request: int
    claims_per_page: int
    request_spacing_s: float
    db: str
    db_path: Path
    turso_url: str | None
    turso_token: str | None
    replica_path: Path
    cache_dir: Path
    cache_enabled: bool
    max_upload_mb: int
    max_upload_pages: int

    @property
    def model(self) -> str:
        return self.ollama_model if self.provider == "ollama" else self.gemini_model


def load(env=None) -> Settings:
    get = (env if env is not None else os.environ).get
    return Settings(
        provider=get("STRATA_PROVIDER", "gemini"),
        gemini_api_key=get("GEMINI_API_KEY") or None,
        gemini_model=get("GEMINI_MODEL", "gemini-3.7-flash"),
        ollama_url=get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=get("OLLAMA_MODEL", "llama3.2-vision"),
        pages_per_request=int(get("STRATA_PAGES_PER_REQUEST", "50")),
        claims_per_page=int(get("STRATA_CLAIMS_PER_PAGE", "8")),
        request_spacing_s=float(get("STRATA_REQUEST_SPACING_S", "12")),
        db=get("STRATA_DB", "sqlite"),
        db_path=_path(get("STRATA_DB_PATH", "data/strata.db")),
        turso_url=get("TURSO_DATABASE_URL") or None,
        turso_token=get("TURSO_AUTH_TOKEN") or None,
        replica_path=_path(get("STRATA_REPLICA_PATH", "data/replica.db")),
        cache_dir=_path(get("STRATA_CACHE_DIR", "data/cache")),
        cache_enabled=get("STRATA_CACHE_ENABLED", "1") == "1",
        max_upload_mb=int(get("STRATA_MAX_UPLOAD_MB", "20")),
        max_upload_pages=int(get("STRATA_MAX_UPLOAD_PAGES", "100")),
    )


settings = load()
