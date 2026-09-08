import json
import re
import time
from datetime import UTC, datetime

import pymupdf

from . import config, db, ingest, providers
from .cache import Cache

BASIS = ("actual", "provisional", "advance_estimate", "revised", "projection", "budget", "pro_forma")
MAX_RESUMES = 4
REGISTRY_LIMIT = 200


def slices(page_count: int, size: int) -> list[tuple[int, int]]:
    return [(first, min(first + size - 1, page_count)) for first in range(1, page_count + 1, size)]


def slice_pdf(pdf_bytes: bytes, first: int, last: int) -> bytes:
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    out.insert_pdf(src, from_page=first - 1, to_page=last - 1)
    out.set_metadata({})
    return out.tobytes(garbage=3, deflate=True)


def metric_key(raw: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    return key or "unknown"


def _hint_lines(pages: list[dict]) -> tuple[list[str], list[int]]:
    lines, skipped = [], []
    for p in pages:
        if p["skipped"]:
            skipped.append(p["page_no"])
        h = p["hints"]
        bits = []
        if h["units"]:
            bits.append("units " + " | ".join(h["units"]))
        if h["scopes"]:
            bits.append("scope " + " | ".join(h["scopes"]))
        if h["periods"]:
            bits.append("period " + " | ".join(h["periods"]))
        if bits:
            lines.append(f"p{p['page_no']}: " + "; ".join(bits))
    return lines, skipped


def build_prompt(
    filename: str, page_count: int, first: int, last: int, pages: list[dict], registry: list[str], cap: int
) -> str:
    hint_lines, skipped = _hint_lines(pages)
    out = [
        f'You are extracting facts from pages {first}-{last} of the document "{filename}" ({page_count} pages).',
        f"Page 1 of the attached PDF is document page {first}. Always report document page numbers.",
        "",
        "Return one JSON object per line and nothing else: no prose, no code fences, no blank lines.",
        "",
    ]
    if first == 1:
        out += [
            "Line 1 describes the document itself:",
            '{"type":"document","title":"...","publisher":"...","published_at":"YYYY-MM-DD, YYYY-MM, YYYY or null"}',
            "publisher is the organisation that issued the document; published_at is when it was published,",
            "taken from the cover, letter, header or footer. Use null when the page does not say.",
            "",
        ]
    else:
        out += ["Do not output a document line; this slice does not start the document.", ""]
    out += [
        "Then one line per fact an analyst would cite:",
        '{"type":"claim","page":<document page>,"subject":"<entity the fact is about>",'
        '"metric":"<snake_case measure>","label":"<the document\'s wording for the measure>",'
        '"value":<number or "short string">,'
        '"unit":"<as stated: INR million, INR crore, percent, USD billion, count>",'
        '"period":"<as printed: FY24, 2024-25, Q4 FY24, March 31, 2024>",'
        '"scope":"<qualifier or null>",'
        '"basis":"<actual|provisional|advance_estimate|revised|projection|budget|pro_forma>",'
        '"quote":"<verbatim text from the page, at most 30 words, containing the value>","keys":{}}',
        "",
        "Rules:",
        "- Facts worth citing: amounts, rates, ratios, growth, counts, dates, appointments and resignations, ratings,",
        "  targets. Skip contents pages, running headers and footers, page numbers, disclaimers and boilerplate.",
        f"- At most {cap} claims per page. Prefer the most material facts on the page.",
        '- subject names the real entity: a company, person, country, institution or segment. Never "the Company",',
        '  "the Bank" or "the Group"; use the name.',
        "- metric names the measure, not the document's label, in snake_case: revenue, net_profit, gdp_growth,",
        "  cpi_inflation, fiscal_deficit_pct_gdp, board_role, headcount. Reuse a key from the registry below when the",
        "  measure is the same.",
        "- label keeps the document's exact wording for the row, chart or phrase.",
        "- value is a number for numeric facts, without thousands separators; figures printed in parentheses are",
        '  negative. Non-numeric facts use a short string such as "Non-Executive Director" or "resigned".',
        "- unit is the unit the page states for that figure. When a table header declares a unit, every figure in",
        "  that table carries it. Use null only when the page truly states none.",
        "- period is the period the figure covers, as printed. Balance-sheet items use the as-at date.",
        "- scope carries qualifiers that change what is measured: consolidated or standalone, a segment or",
        '  subsidiary, and footnote exclusions such as "excludes revenue from traded goods". null when none.',
        "- basis is advance_estimate, provisional or revised when the document labels the figure so; projection or",
        "  budget for forward-looking figures; pro_forma for restated comparatives; otherwise actual.",
        "- quote is copied verbatim from the page text and contains the value exactly as printed. Never paraphrase.",
        "- keys holds identifiers printed with the entity, such as"
        ' {"din":"01173669"} or {"cin":"L63090DL2011PLC221234"}.',
        "",
    ]
    if hint_lines:
        out += ["Units, scope and period headers detected on these pages:", *hint_lines, ""]
    if skipped:
        out += [
            "Pages with almost no text (covers, dividers): "
            + ", ".join(map(str, skipped))
            + ". Output nothing for them.",
            "",
        ]
    out.append("Metric registry so far: " + (", ".join(registry) if registry else "(empty)"))
    return "\n".join(out)


def parse(text: str) -> tuple[dict | None, list[dict], int]:
    document, claims, malformed = None, [], 0
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            items = json.loads(stripped)
            lines = [json.dumps(item) for item in items]
        except json.JSONDecodeError:
            lines = text.splitlines()
    else:
        lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(obj, dict):
            malformed += 1
            continue
        kind = obj.get("type")
        if kind == "document":
            document = obj
        elif kind == "claim" and isinstance(obj.get("page"), int) and obj.get("quote") and obj.get("metric"):
            claims.append(obj)
        else:
            malformed += 1
    return document, claims, malformed


def _value_raw(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return repr(value)
    return str(value)


def _as_text(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def insert_claims(conn, doc_id: int, claims: list[dict]) -> int:
    rows = []
    for c in claims:
        basis = str(c.get("basis") or "actual").lower()
        rows.append(
            (
                doc_id,
                c["page"],
                _as_text(c.get("subject")),
                metric_key(str(c.get("metric"))),
                _as_text(c.get("label")),
                _value_raw(c.get("value")),
                _as_text(c.get("unit")),
                _as_text(c.get("period")),
                _as_text(c.get("scope")),
                basis if basis in BASIS else "actual",
                str(c.get("quote")),
                json.dumps(c.get("keys") or {}, ensure_ascii=False),
            )
        )
    conn.executemany(
        "insert into claims (doc_id, page_no, subject, metric_raw, label, value_raw, unit_raw, period_raw, scope_raw,"
        " basis_raw, quote, keys_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    for row in rows:
        conn.execute(
            "insert into metrics (key, label, first_seen_doc, claim_count) values (?, ?, ?, 1)"
            " on conflict(key) do update set claim_count = claim_count + 1",
            (row[3], row[4], doc_id),
        )
    return len(rows)


def cache_key(cache: Cache, provider, prompt: str, sha256: str, first: int, last: int) -> str:
    stem = prompt.rsplit("\nMetric registry so far:", 1)[0]
    return cache.key(provider.name, provider.model, stem, sha256, f"{first}-{last}")


def clear_document(conn, doc_id: int):
    counts = db.rows(
        conn, "select metric_raw as key, count(*) as n from claims where doc_id = ? group by metric_raw", (doc_id,)
    )
    conn.execute(
        "delete from relations where a_id in (select id from claims where doc_id = ?)"
        " or b_id in (select id from claims where doc_id = ?)",
        (doc_id, doc_id),
    )
    for table in ("claim_canon", "evidence", "quarantine"):
        conn.execute(f"delete from {table} where claim_id in (select id from claims where doc_id = ?)", (doc_id,))
    conn.execute("delete from claims where doc_id = ?", (doc_id,))
    for row in counts:
        conn.execute("update metrics set claim_count = max(claim_count - ?, 0) where key = ?", (row["n"], row["key"]))
    conn.execute(
        "delete from metrics where claim_count <= 0"
        " and key not in (select metric_key from claim_canon where metric_key is not null)"
        " and key not in (select metric_raw from claims)"
    )
    conn.execute("update documents set malformed_lines = 0 where id = ?", (doc_id,))


def registry(conn) -> list[str]:
    return [
        r["key"]
        for r in db.rows(conn, "select key from metrics order by claim_count desc, key limit ?", (REGISTRY_LIMIT,))
    ]


def apply_document_line(conn, doc_id: int, document: dict | None):
    if not document:
        return
    fields = {k: document.get(k) for k in ("title", "publisher", "published_at") if isinstance(document.get(k), str)}
    if fields:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"update documents set {assignments} where id = ?", (*fields.values(), doc_id))


def extract_document(conn, doc_id: int, provider=None, cache: Cache | None = None, cfg=None) -> dict:
    cfg = cfg or config.settings
    provider = provider or providers.get_provider()
    cache = cache or Cache()
    doc = db.one(conn, "select id, sha256, filename, pdf, page_count from documents where id = ?", (doc_id,))
    pages = {p["page_no"]: p for p in ingest.pages(conn, doc_id)}
    stats = {"requests": 0, "cached": 0, "claims": 0, "malformed": 0, "resumes": 0, "finish": [], "tokens_out": 0}
    started = time.perf_counter()
    clear_document(conn, doc_id)
    conn.execute("update documents set status = 'extracting', model = ? where id = ?", (provider.model, doc_id))
    db.commit(conn)
    status = "extracted"
    for first, last in slices(doc["page_count"], cfg.pages_per_request):
        start = first
        resumes = 0
        while start <= last:
            window = [pages[n] for n in range(start, last + 1)]
            prompt = build_prompt(
                doc["filename"], doc["page_count"], start, last, window, registry(conn), cfg.claims_per_page
            )
            key = cache_key(cache, provider, prompt, doc["sha256"], start, last)
            try:
                response = providers.generate(slice_pdf(doc["pdf"], start, last), prompt, provider, cache, key)
            except Exception as e:
                if getattr(e, "code", None) == 429:
                    status = "quota_exhausted"
                    break
                raise
            stats["requests"] += 1
            stats["cached"] += int(response.cached)
            stats["finish"].append(response.finish_reason)
            stats["tokens_out"] += response.output_tokens or 0
            document, claims, malformed = parse(response.text)
            next_start = last + 1
            if response.finish_reason == "MAX_TOKENS" and claims:
                cut = max(c["page"] for c in claims)
                claims = [c for c in claims if c["page"] < cut]
                next_start = cut
            elif response.finish_reason == "MAX_TOKENS":
                status = "partial"
                break
            stats["claims"] += insert_claims(conn, doc_id, claims)
            stats["malformed"] += malformed
            if start == first:
                apply_document_line(conn, doc_id, document)
            conn.execute("update documents set malformed_lines = malformed_lines + ? where id = ?", (malformed, doc_id))
            db.commit(conn)
            if next_start <= start or resumes >= MAX_RESUMES:
                if next_start <= last:
                    status = "partial"
                break
            if next_start <= last:
                resumes += 1
                stats["resumes"] += 1
            start = next_start
        if status in ("quota_exhausted", "partial"):
            break
    conn.execute(
        "update documents set status = ?, ingested_at = ? where id = ?",
        (status, datetime.now(UTC).isoformat(timespec="seconds"), doc_id),
    )
    db.commit(conn)
    stats["status"] = status
    stats["seconds"] = round(time.perf_counter() - started, 2)
    return stats
