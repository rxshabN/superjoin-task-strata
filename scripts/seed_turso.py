import argparse
import base64
import dataclasses
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

from strata import config, db

TABLES = (
    "documents",
    "pages",
    "claims",
    "evidence",
    "quarantine",
    "entities",
    "metrics",
    "claim_canon",
    "relations",
    "metric_aliases",
)
BATCH = 150


def columns(conn, table: str) -> list[str]:
    return [r["name"] for r in db.rows(conn, f"pragma table_info({table})")]


def hrana(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    return {"type": "text", "value": str(value)}


class Pipeline:
    def __init__(self, url: str, token: str):
        host = url.replace("libsql://", "https://").replace("wss://", "https://").rstrip("/")
        self.endpoint = f"{host}/v2/pipeline"
        self.token = token

    def run(self, statements: list[tuple[str, tuple]]) -> list[dict]:
        requests = [
            {"type": "execute", "stmt": {"sql": sql, "args": [hrana(a) for a in args]}} for sql, args in statements
        ]
        body = json.dumps({"requests": [*requests, {"type": "close"}]}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            results = json.load(response)["results"]
        for i, result in enumerate(results):
            if result.get("type") == "error":
                raise RuntimeError(f"statement {i}: {result['error'].get('message')}")
        return results

    def count(self, table: str) -> int:
        result = self.run([(f"select count(*) from {table}", ())])[0]
        return int(result["response"]["result"]["rows"][0][0]["value"])


def seed_pipeline(src, pipeline: Pipeline):
    pipeline.run([(f"delete from {table}", ()) for table in reversed(TABLES)])
    for table in TABLES:
        cols = columns(src, table)
        rows = src.execute(f"select {', '.join(cols)} from {table}").fetchall()
        sql = f"insert into {table} ({', '.join(cols)}) values ({', '.join('?' * len(cols))})"
        size = 1 if table == "documents" else BATCH
        t = time.perf_counter()
        for start in range(0, len(rows), size):
            pipeline.run([(sql, tuple(row)) for row in rows[start : start + size]])
        print(f"{table:15} {len(rows):6} rows {time.perf_counter() - t:6.1f}s", flush=True)
    print("\nverifying")
    for table in TABLES:
        want = src.execute(f"select count(*) from {table}").fetchone()[0]
        got = pipeline.count(table)
        print(f"  {table:15} {got:6} / {want:6}{'' if want == got else '  MISMATCH'}")


def seed_sqlite(src, dst):
    for table in reversed(TABLES):
        dst.execute(f"delete from {table}")
    dst.commit()
    for table in TABLES:
        cols = columns(src, table)
        rows = src.execute(f"select {', '.join(cols)} from {table}").fetchall()
        dst.executemany(f"insert into {table} ({', '.join(cols)}) values ({', '.join('?' * len(cols))})", rows)
        dst.commit()
        print(f"{table:15} {len(rows):6} rows")
    print("\nverifying")
    for table in TABLES:
        want = src.execute(f"select count(*) from {table}").fetchone()[0]
        got = db.one(dst, f"select count(*) as n from {table}")["n"]
        print(f"  {table:15} {got:6} / {want:6}{'' if want == got else '  MISMATCH'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="seed a local SQLite file instead of Turso, as a dry run")
    args = parser.parse_args()
    cfg = config.settings
    src = sqlite3.connect(str(cfg.db_path))
    started = time.perf_counter()
    if args.target:
        seed_sqlite(src, db.init(db.connect(cfg, path=Path(args.target))))
    else:
        turso = dataclasses.replace(cfg, db="turso")
        db.init(db.connect(turso)).close()
        seed_pipeline(src, Pipeline(turso.turso_url, turso.turso_token))
    print(f"\n{time.perf_counter() - started:.1f}s total")


if __name__ == "__main__":
    main()
