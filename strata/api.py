from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db

WEB = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.init(db.connect())
    app.state.db = conn
    yield
    close = getattr(conn, "close", None)
    if close:
        close()


app = FastAPI(title="Strata", version=__version__, lifespan=lifespan)


@app.get("/health")
def health():
    cfg = config.settings
    return {
        "ok": True,
        "version": __version__,
        "provider": cfg.provider,
        "model": cfg.model,
        "db": db.backend(cfg),
        "tables": len(db.tables(app.state.db)),
        "cache": cfg.cache_enabled,
    }


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
