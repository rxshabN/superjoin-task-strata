import time

from strata import config, db
from strata.ingest import ingest_path, summary


def main():
    root = config.ROOT / "starter-datasets"
    conn = db.init(db.connect())
    started = time.perf_counter()
    header = ("document", "pages", "skip", "notext", "units", "scope", "period", "secs", "new")
    print(f"{header[0]:50} " + " ".join(f"{h:>6}" for h in header[1:]))
    totals = {"pages": 0, "skipped": 0}
    for path in sorted(root.rglob("*.pdf")):
        result = ingest_path(conn, path)
        s = summary(conn, result["id"])
        totals["pages"] += s["pages"]
        totals["skipped"] += s["skipped"]
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
    elapsed = time.perf_counter() - started
    print(f"\n{totals['pages']} pages, {totals['skipped']} skipped, {elapsed:.1f}s total, db={db.backend()}")


if __name__ == "__main__":
    main()
