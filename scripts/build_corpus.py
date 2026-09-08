import argparse
import logging
import time

from strata import config, db, providers
from strata.cache import Cache
from strata.canon import canonicalise_document, reset
from strata.extract import extract_document
from strata.ingest import ingest_path, summary
from strata.reconcile import reconcile
from strata.verify import reverify_document, verify_document

EXTRACTABLE = ("ingested", "extracting", "partial", "quota_exhausted")
MAX_PASSES = 8
PASS_WAIT_S = 900


def ingest_all(conn, only: str | None) -> list[int]:
    root = config.ROOT / "starter-datasets"
    header = ("document", "pages", "skip", "notext", "units", "scope", "period", "secs", "new")
    print(f"{header[0]:50} " + " ".join(f"{h:>6}" for h in header[1:]))
    ids = []
    for path in sorted(root.rglob("*.pdf")):
        if only and only not in path.name:
            continue
        result = ingest_path(conn, path)
        ids.append(result["id"])
        s = summary(conn, result["id"])
        cells = (
            s["pages"],
            s["skipped"],
            s["no_text_layer"],
            s["unit_pages"],
            s["scope_pages"],
            s["period_pages"],
            f"{result.get('seconds', 0):.2f}",
            "yes" if result["new"] else "no",
        )
        print(f"{path.name[:50]:50} " + " ".join(f"{c:>6}" for c in cells))
    return ids


def extract_all(conn, ids: list[int]):
    provider = providers.get_provider()
    cache = Cache()
    print(f"\nprovider={provider.name} model={provider.model} pages/request={config.settings.pages_per_request}")
    for doc_id in ids:
        doc = db.one(conn, "select filename, status from documents where id = ?", (doc_id,))
        if doc["status"] not in EXTRACTABLE:
            print(f"\n{doc['filename']}: status {doc['status']}, skipping")
            continue
        print(f"\n{doc['filename']}: extracting", flush=True)
        try:
            stats = extract_document(conn, doc_id, provider, cache)
        except Exception as error:
            conn.execute("update documents set status = 'ingested' where id = ?", (doc_id,))
            db.commit(conn)
            print(f"  failed: {type(error).__name__}: {str(error)[:200]}", flush=True)
            if getattr(error, "code", None) == 503:
                print("  model unavailable; ending this pass", flush=True)
                return
            continue
        print(
            f"  requests={stats['requests']} cached={stats['cached']} finish={stats['finish']} "
            f"tokens_out={stats['tokens_out']} claims={stats['claims']} malformed={stats['malformed']} "
            f"resumes={stats['resumes']} status={stats['status']} {stats['seconds']}s"
        )
        verified = verify_document(conn, doc_id)
        print(
            f"  exact={verified['exact']} nearby={verified['nearby']} tokens={verified['tokens']} "
            f"quarantine={verified['quarantine']} {verified['reasons']}"
        )


def reconcile_all(conn, ids: list[int]):
    reset(conn)
    print("\ncanonicalising")
    for doc_id in ids:
        doc = db.one(conn, "select filename, status from documents where id = ?", (doc_id,))
        if doc["status"] != "extracted":
            continue
        stats = canonicalise_document(conn, doc_id)
        cells = " ".join(f"{k}={v}" for k, v in stats.items())
        print(f"  {doc['filename'][:50]:50} {cells}")
    stats = reconcile(conn)
    print(f"\nreconciled: {stats}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--until-done", action="store_true")
    parser.add_argument("--reverify", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.init(db.connect())
    started = time.perf_counter()
    ids = ingest_all(conn, args.only)
    if args.extract:
        for attempt in range(1, MAX_PASSES + 1):
            extract_all(conn, ids)
            pending = [
                i
                for i in ids
                if db.one(conn, "select status from documents where id = ?", (i,))["status"] in EXTRACTABLE
            ]
            if not pending or not args.until_done or attempt == MAX_PASSES:
                break
            print(f"\n{len(pending)} document(s) pending after pass {attempt}; waiting {PASS_WAIT_S}s", flush=True)
            time.sleep(PASS_WAIT_S)
    if args.reverify:
        print("\nre-verifying quarantined claims")
        for doc_id in ids:
            stats = reverify_document(conn, doc_id)
            name = db.one(conn, "select filename from documents where id = ?", (doc_id,))["filename"]
            print(f"  {name[:50]:50} {stats}")
    if args.reconcile:
        reconcile_all(conn, ids)
    print(f"\n{time.perf_counter() - started:.1f}s total, db={db.backend()}")


if __name__ == "__main__":
    main()
