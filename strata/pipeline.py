import logging
import threading
from datetime import UTC, datetime

from . import canon, db, extract, providers, reconcile, verify

log = logging.getLogger("strata.pipeline")
LOCK = threading.Lock()
IN_FLIGHT = ("extracting", "verifying", "reconciling")
PENDING = ("ingested", *IN_FLIGHT, "partial", "quota_exhausted", "failed")
DONE = ("extracted", "ready")
STALE_S = 900


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def in_flight(status: str, updated_at: str | None) -> bool:
    if status not in IN_FLIGHT or not updated_at:
        return False
    try:
        touched = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    return (datetime.now(UTC) - touched).total_seconds() < STALE_S


def _status(conn, doc_id: int, status: str, error: str | None = None):
    if error is None:
        conn.execute("update documents set status = ?, updated_at = ? where id = ?", (status, now(), doc_id))
    else:
        conn.execute(
            "update documents set status = ?, error = ?, updated_at = ? where id = ?", (status, error, now(), doc_id)
        )
    db.commit(conn)


def claim(conn, doc_id: int) -> bool:
    marks = ",".join("?" * len(PENDING))
    cur = conn.execute(
        f"update documents set status = 'extracting', updated_at = ? where id = ? and status in ({marks})",
        (now(), doc_id, *PENDING),
    )
    db.commit(conn)
    return cur.rowcount == 1


def process_document(doc_id: int, api_key: str | None = None) -> dict:
    with LOCK:
        conn = db.connect(worker=True)
        try:
            if not claim(conn, doc_id):
                log.info("document %s is not pending, skipping", doc_id)
                return {"status": "skipped"}
            if api_key:
                provider = providers.get_provider("gemini", api_key=api_key, backend="aistudio")
            else:
                provider = providers.get_provider()
            stats = extract.extract_document(conn, doc_id, provider)
            if stats["status"] != "extracted":
                return stats
            _status(conn, doc_id, "verifying")
            stats["verify"] = verify.verify_document(conn, doc_id)
            _status(conn, doc_id, "reconciling")
            stats["canon"] = canon.canonicalise_document(conn, doc_id)
            stats["reconcile"] = reconcile.reconcile(conn, reconcile.blocks_for_document(conn, doc_id))
            _status(conn, doc_id, "ready")
            return stats
        except Exception as error:
            log.exception("document %s failed", doc_id)
            _status(conn, doc_id, "failed", f"{type(error).__name__}: {str(error)[:300]}")
            return {"status": "failed", "error": str(error)}
        finally:
            close = getattr(conn, "close", None)
            if close:
                close()
