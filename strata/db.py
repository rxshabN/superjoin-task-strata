import sqlite3
from pathlib import Path

from . import config

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def backend(cfg: config.Settings | None = None) -> str:
    cfg = cfg or config.settings
    return "turso" if cfg.db == "turso" else "sqlite"


def connect(cfg: config.Settings | None = None, path: Path | None = None, worker: bool = False):
    cfg = cfg or config.settings
    if backend(cfg) == "turso":
        if not (cfg.turso_url and cfg.turso_token):
            raise RuntimeError("STRATA_DB=turso needs TURSO_DATABASE_URL and TURSO_AUTH_TOKEN")
        import libsql

        replica = path or cfg.replica_path
        if worker and path is None:
            replica = replica.with_name(f"{replica.stem}-worker{replica.suffix}")
        replica.parent.mkdir(parents=True, exist_ok=True)
        connect = libsql.connect
        conn = connect(
            str(replica), sync_url=cfg.turso_url, auth_token=cfg.turso_token, isolation_level=None, sync_interval=30
        )
        conn.sync()
        return conn
    target = path or cfg.db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.execute("pragma foreign_keys = on")
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    for table, column, kind in (
        ("claim_canon", "precision", "real"),
        ("documents", "model", "text"),
        ("documents", "error", "text"),
        ("documents", "updated_at", "text"),
    ):
        columns = {r["name"] for r in rows(conn, f"pragma table_info({table})")}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {kind}")
    conn.commit()
    return conn


def refresh(conn):
    sync = getattr(conn, "sync", None)
    if sync:
        try:
            sync()
        except ValueError:
            pass


def commit(conn):
    conn.commit()
    refresh(conn)


def rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [d[0].lower() for d in cur.description] if cur.description else []
    return [dict(zip(names, r, strict=False)) for r in cur.fetchall()]


def one(conn, sql: str, params=()) -> dict | None:
    found = rows(conn, sql, params)
    return found[0] if found else None


def tables(conn) -> list[str]:
    return [r["name"] for r in rows(conn, "select name from sqlite_master where type = 'table' order by name")]
