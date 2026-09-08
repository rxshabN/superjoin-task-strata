import logging
import threading

from . import canon, db, extract, providers, reconcile, verify

log = logging.getLogger("strata.pipeline")
LOCK = threading.Lock()
PENDING = ("ingested", "extracting", "verifying", "reconciling", "partial", "quota_exhausted", "failed")
DONE = ("extracted", "ready")


def _status(conn, doc_id: int, status: str, error: str | None = None):
    if error is None:
        conn.execute("update documents set status = ? where id = ?", (status, doc_id))
    else:
        conn.execute("update documents set status = ?, error = ? where id = ?", (status, error, doc_id))
    db.commit(conn)


def process_document(doc_id: int, api_key: str | None = None) -> dict:
    with LOCK:
        conn = db.connect(worker=True)
        try:
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
