import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from . import db

THIN_WORDS = 40
THIN_NUMBERS = 3
MAX_HINTS = 3

CURRENCY = {
    "indian rupees": "INR",
    "indian rupee": "INR",
    "rupees": "INR",
    "rupee": "INR",
    "rs": "INR",
    "inr": "INR",
    "₹": "INR",
    "us$": "USD",
    "usd": "USD",
    "$": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}

SCALE = {
    "million": "million",
    "millions": "million",
    "mn": "million",
    "crore": "crore",
    "crores": "crore",
    "cr": "crore",
    "lakh": "lakh",
    "lakhs": "lakh",
    "billion": "billion",
    "billions": "billion",
    "bn": "billion",
    "thousand": "thousand",
    "thousands": "thousand",
    "per cent": "percent",
    "percent": "percent",
    "%": "percent",
}

DECL_RE = re.compile(
    r"(?:\(|^)\s*(?:all\s+)?(?:amounts?|figures?|values?)?\s*(?:are\s+)?(?:in\s+)?"
    r"(?P<cur>indian\s+rupees?|rupees?|us\$|usd|rs\.?|inr|₹|\$|€|eur|£|gbp)?\s*(?:in\s+)?"
    r"(?P<scale>millions?|mn|crores?|cr|lakhs?|billions?|bn|thousands?|per\s?cent|percent|%)(?![\w.])",
    re.I | re.M,
)

SCOPE_RE = re.compile(r"\b(consolidated|standalone)\b", re.I)

PERIOD_RE = re.compile(
    r"\b(?P<kind>for the (?:year|quarter|half[- ]year|period|nine months|six months|three months) ended"
    r"|year ended|quarter ended|as at|as on)\s+"
    r"(?P<date>[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+\d{4})",
    re.I,
)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _unique(items, limit=MAX_HINTS) -> list[str]:
    seen = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
        if len(seen) == limit:
            break
    return seen


def detect_units(text: str) -> list[str]:
    found = []
    for m in DECL_RE.finditer(text):
        cur = _squash(m.group("cur") or "").rstrip(".")
        scale = SCALE[_squash(m.group("scale"))]
        currency = CURRENCY.get(cur)
        found.append(f"{currency} {scale}" if currency and scale != "percent" else scale)
    return _unique(found)


def detect_scopes(text: str) -> list[str]:
    return _unique(m.group(1).lower() for m in SCOPE_RE.finditer(text))


def detect_periods(text: str) -> list[str]:
    return _unique(_squash(m.group(0)) for m in PERIOD_RE.finditer(text))


def detect_hints(text: str) -> dict:
    return {
        "units": detect_units(text),
        "scopes": detect_scopes(text),
        "periods": detect_periods(text),
        "text_layer": bool(text.strip()),
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def thin(text: str, words: int) -> bool:
    return words < THIN_WORDS and len(re.findall(r"\d[\d,.]*", text)) < THIN_NUMBERS


def page_records(doc) -> list[tuple]:
    records = []
    for page_no, page in enumerate(doc, start=1):
        text = page.get_text()
        words = len(text.split())
        hints = detect_hints(text)
        records.append((page_no, text, words, json.dumps(hints, ensure_ascii=False), int(thin(text, words))))
    return records


def ingest_bytes(conn, pdf_bytes: bytes, filename: str) -> dict:
    sha = sha256(pdf_bytes)
    existing = db.one(conn, "select id, page_count, status from documents where sha256 = ?", (sha,))
    if existing:
        return {
            "id": existing["id"],
            "sha256": sha,
            "filename": filename,
            "page_count": existing["page_count"],
            "status": existing["status"],
            "new": False,
        }
    started = time.perf_counter()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    records = page_records(doc)
    conn.execute(
        "insert into documents (sha256, filename, pdf, page_count, status, ingested_at) values (?, ?, ?, ?, ?, ?)",
        (sha, filename, pdf_bytes, len(records), "ingested", datetime.now(UTC).isoformat(timespec="seconds")),
    )
    doc_id = db.one(conn, "select id from documents where sha256 = ?", (sha,))["id"]
    conn.executemany(
        "insert into pages (doc_id, page_no, text, word_count, hints_json, skipped) values (?, ?, ?, ?, ?, ?)",
        [(doc_id, *record) for record in records],
    )
    db.commit(conn)
    return {
        "id": doc_id,
        "sha256": sha,
        "filename": filename,
        "page_count": len(records),
        "skipped": sum(record[4] for record in records),
        "seconds": round(time.perf_counter() - started, 2),
        "status": "ingested",
        "new": True,
    }


def ingest_path(conn, path: Path) -> dict:
    return ingest_bytes(conn, path.read_bytes(), path.name)


def pages(conn, doc_id: int) -> list[dict]:
    found = db.rows(
        conn,
        "select page_no, text, word_count, hints_json, skipped from pages where doc_id = ? order by page_no",
        (doc_id,),
    )
    for row in found:
        row["hints"] = json.loads(row.pop("hints_json"))
    return found


def summary(conn, doc_id: int) -> dict:
    found = pages(conn, doc_id)
    return {
        "pages": len(found),
        "skipped": sum(p["skipped"] for p in found),
        "no_text_layer": sum(1 for p in found if not p["hints"]["text_layer"]),
        "unit_pages": sum(1 for p in found if p["hints"]["units"]),
        "scope_pages": sum(1 for p in found if p["hints"]["scopes"]),
        "period_pages": sum(1 for p in found if p["hints"]["periods"]),
    }
