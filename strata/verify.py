import json
import re
import unicodedata

import pymupdf

from . import db, ingest

GRADES = ("exact", "nearby", "tokens")
WORD_SHARE = 0.7


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(r"\(\d{1,2}(?:,\s*\d{1,2})*\)", "", s)
    s = re.sub(r"(?<=[a-z])\d{1,2}(?=[\s,.;:)]|$)", "", s)
    s = re.sub(r"[^\w%₹$.,-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def tokens_present(quote_norm: str, page_norm: str) -> bool:
    numbers = re.findall(r"\d[\d,]*\.?\d*", quote_norm)
    words = re.findall(r"[a-z]{3,}", quote_norm)
    if not numbers:
        return False
    if not all(n in page_norm for n in numbers):
        return False
    if not words:
        return True
    return sum(w in page_norm for w in words) >= WORD_SHARE * len(words)


def grade(quote: str, page_text: str, prev_text: str = "", next_text: str = "") -> tuple[str | None, int]:
    q = normalize(quote)
    if not q:
        return None, 0
    page = normalize(page_text)
    if q in page:
        return "exact", 0
    if prev_text and q in normalize(prev_text):
        return "nearby", -1
    if next_text and q in normalize(next_text):
        return "nearby", 1
    if tokens_present(q, page):
        return "tokens", 0
    if prev_text and tokens_present(q, normalize(prev_text)):
        return "tokens", -1
    if next_text and tokens_present(q, normalize(next_text)):
        return "tokens", 1
    return None, 0


def char_span(quote: str, page_text: str) -> tuple[int, int] | None:
    parts = [re.escape(t) for t in quote.split()]
    if not parts:
        return None
    pattern = r"[\s\S]{0,4}?".join(parts)
    m = re.search(pattern, page_text, re.I)
    return (m.start(), m.end()) if m else None


def bboxes(page, quote: str) -> list[list[float]]:
    rects = page.search_for(quote)
    if not rects:
        words = quote.split()
        if len(words) > 6:
            rects = page.search_for(" ".join(words[:6]))
    return [[round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)] for r in rects]


def verify_document(conn, doc_id: int) -> dict:
    doc = db.one(conn, "select pdf, page_count from documents where id = ?", (doc_id,))
    pages = {p["page_no"]: p for p in ingest.pages(conn, doc_id)}
    pdf = pymupdf.open(stream=doc["pdf"], filetype="pdf")
    claims = db.rows(
        conn, "select id, page_no, quote from claims where doc_id = ? and status = 'unverified' order by id", (doc_id,)
    )
    stats = {"exact": 0, "nearby": 0, "tokens": 0, "quarantine": 0, "reasons": {}}
    for claim in claims:
        page_no = claim["page_no"]
        if page_no < 1 or page_no > doc["page_count"]:
            _quarantine(conn, claim["id"], "page_out_of_range", f"page {page_no} of {doc['page_count']}", stats)
            continue
        text = pages[page_no]["text"]
        prev_text = pages[page_no - 1]["text"] if page_no > 1 else ""
        next_text = pages[page_no + 1]["text"] if page_no < doc["page_count"] else ""
        found, offset = grade(claim["quote"], text, prev_text, next_text)
        if found is None:
            reason = "no_text_layer" if not pages[page_no]["hints"]["text_layer"] else "quote_not_found"
            _quarantine(conn, claim["id"], reason, claim["quote"][:200], stats)
            continue
        located = page_no + offset
        span = char_span(claim["quote"], pages[located]["text"])
        boxes = bboxes(pdf[located - 1], claim["quote"]) if found != "tokens" else []
        conn.execute(
            "insert into evidence (claim_id, page_no, char_start, char_end, bbox_json, grade)"
            " values (?, ?, ?, ?, ?, ?)",
            (claim["id"], located, span[0] if span else None, span[1] if span else None, json.dumps(boxes), found),
        )
        conn.execute("update claims set status = 'verified', page_no = ? where id = ?", (located, claim["id"]))
        stats[found] += 1
    db.commit(conn)
    return stats


def reverify_document(conn, doc_id: int) -> dict:
    conn.execute(
        "delete from quarantine where claim_id in (select id from claims where doc_id = ? and status = 'quarantined')",
        (doc_id,),
    )
    conn.execute("update claims set status = 'unverified' where doc_id = ? and status = 'quarantined'", (doc_id,))
    db.commit(conn)
    return verify_document(conn, doc_id)


def _quarantine(conn, claim_id: int, reason: str, detail: str, stats: dict):
    conn.execute("insert into quarantine (claim_id, reason, detail) values (?, ?, ?)", (claim_id, reason, detail))
    conn.execute("update claims set status = 'quarantined' where id = ?", (claim_id,))
    stats["quarantine"] += 1
    stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
