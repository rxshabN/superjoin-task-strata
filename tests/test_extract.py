import dataclasses
import json

import pymupdf
import pytest

from strata import config, db
from strata.cache import Cache
from strata.extract import build_prompt, extract_document, metric_key, parse, slice_pdf, slices
from strata.ingest import ingest_bytes
from strata.providers.base import Response


def test_slices():
    assert slices(27, 50) == [(1, 27)]
    assert slices(100, 50) == [(1, 50), (51, 100)]
    assert slices(101, 50) == [(1, 50), (51, 100), (101, 101)]


def test_metric_key():
    assert metric_key("Revenue from Operations") == "revenue_from_operations"
    assert metric_key(" GDP-growth ") == "gdp_growth"
    assert metric_key("") == "unknown"


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    return doc.tobytes()


def test_slice_pdf_keeps_page_range():
    pdf = make_pdf([f"page {i}" for i in range(1, 6)])
    part = pymupdf.open(stream=slice_pdf(pdf, 2, 4), filetype="pdf")
    assert len(part) == 3
    assert "page 2" in part[0].get_text() and "page 4" in part[2].get_text()


def test_build_prompt_mentions_pages_hints_and_registry():
    pages = [
        {
            "page_no": 51,
            "skipped": 0,
            "hints": {"units": ["INR million"], "scopes": ["consolidated"], "periods": [], "text_layer": True},
        },
        {"page_no": 52, "skipped": 1, "hints": {"units": [], "scopes": [], "periods": [], "text_layer": True}},
    ]
    prompt = build_prompt("ar.pdf", 100, 51, 100, pages, ["revenue", "net_profit"], 8)
    assert "pages 51-100" in prompt and "document page 51" in prompt
    assert "Do not output a document line" in prompt
    assert "p51: units INR million; scope consolidated" in prompt
    assert "dividers): 52." in prompt
    assert "At most 8 claims per page" in prompt
    assert prompt.endswith("Metric registry so far: revenue, net_profit")
    first = build_prompt("ar.pdf", 100, 1, 50, [], [], 8)
    assert '"type":"document"' in first and first.endswith("(empty)")


def test_parse_handles_noise():
    text = "\n".join(
        [
            "```json",
            '{"type":"document","title":"T","publisher":"P","published_at":"2024-07"}',
            '{"type":"claim","page":3,"subject":"X","metric":"revenue","value":10,"quote":"revenue 10"}',
            "not json",
            '{"type":"claim","page":"3","quote":"missing metric"}',
            '{"type":"claim","page":4,"subject":"X","metric":"cost","value":5,"quote":"cost 5",',
            "```",
        ]
    )
    document, claims, malformed = parse(text)
    assert document["title"] == "T"
    assert [c["page"] for c in claims] == [3]
    assert malformed == 3


def test_parse_accepts_array():
    document, claims, malformed = parse('[{"type":"claim","page":1,"metric":"m","quote":"q","value":1}]')
    assert document is None and len(claims) == 1 and malformed == 0


class ScriptedProvider:
    name = "scripted"
    model = "s-1"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, pdf_bytes, prompt):
        self.prompts.append(prompt)
        text, finish = self.responses.pop(0)
        return Response(text=text, finish_reason=finish, output_tokens=len(text), model=self.model)


def claim(page, metric, value, quote):
    return json.dumps(
        {
            "type": "claim",
            "page": page,
            "subject": "Acme",
            "metric": metric,
            "value": value,
            "unit": "INR crore",
            "period": "FY24",
            "quote": quote,
        }
    )


def test_extract_document_resumes_after_truncation(tmp_path):
    cfg = dataclasses.replace(config.load({}), pages_per_request=3, claims_per_page=8)
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    pdf = make_pdf([" ".join(["text"] * 50) + f" page {i}" for i in range(1, 5)])
    doc = ingest_bytes(conn, pdf, "acme.pdf")
    first = "\n".join(
        [
            '{"type":"document","title":"Acme Annual Report","publisher":"Acme Ltd","published_at":"2024-06"}',
            claim(1, "revenue", 100, "revenue 100"),
            claim(2, "net_profit", 10, "net profit 10"),
            claim(2, "headcount", 5, "headcount 5"),
            '{"type":"claim","page":3,"subject":"Acme","metric":"cost","value":4',
        ]
    )
    second = "\n".join(
        [
            claim(2, "net_profit", 10, "net profit 10"),
            claim(2, "headcount", 5, "headcount 5"),
            claim(3, "cost", 4, "cost 4"),
        ]
    )
    third = claim(4, "assets", 7, "assets 7")
    provider = ScriptedProvider([(first, "MAX_TOKENS"), (second, "STOP"), (third, "STOP")])
    cache = Cache(tmp_path / "cache", enabled=True)
    stats = extract_document(conn, doc["id"], provider, cache, cfg)
    assert stats["requests"] == 3 and stats["resumes"] == 1 and stats["status"] == "extracted"
    assert stats["claims"] == 5 and stats["malformed"] == 1
    assert "pages 2-3" in provider.prompts[1] and "pages 4-4" in provider.prompts[2]
    assert "Metric registry so far: revenue" in provider.prompts[1]
    rows = db.rows(conn, "select page_no, metric_raw, value_raw from claims order by id")
    assert [(r["page_no"], r["metric_raw"]) for r in rows] == [
        (1, "revenue"),
        (2, "net_profit"),
        (2, "headcount"),
        (3, "cost"),
        (4, "assets"),
    ]
    document = db.one(conn, "select title, publisher, published_at, status, malformed_lines from documents")
    assert document == {
        "title": "Acme Annual Report",
        "publisher": "Acme Ltd",
        "published_at": "2024-06",
        "status": "extracted",
        "malformed_lines": 1,
    }
    metrics = {r["key"]: r["claim_count"] for r in db.rows(conn, "select key, claim_count from metrics")}
    assert metrics == {"revenue": 1, "net_profit": 1, "headcount": 1, "cost": 1, "assets": 1}


def test_reextract_replaces_claims_and_ignores_registry_drift(tmp_path):
    cfg = dataclasses.replace(config.load({}), pages_per_request=1)
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    pdf = make_pdf([" ".join(["text"] * 50), " ".join(["more"] * 50)])
    doc = ingest_bytes(conn, pdf, "two.pdf")
    cache = Cache(tmp_path / "cache", enabled=True)
    partial = ScriptedProvider([(claim(1, "revenue", 1, "revenue 1"), "STOP")])
    with pytest.raises(IndexError):
        extract_document(conn, doc["id"], partial, cache, cfg)
    assert db.one(conn, "select count(*) as n from claims")["n"] == 1
    other = ingest_bytes(conn, make_pdf(["other document"]), "other.pdf")
    conn.execute(
        "insert into claims (doc_id, page_no, metric_raw, quote, status)"
        " values (?, 1, 'unrelated_key', 'q', 'verified')",
        (other["id"],),
    )
    conn.execute("insert into metrics (key, claim_count) values ('unrelated_key', 1)")
    conn.commit()
    resumed = ScriptedProvider([(claim(2, "cost", 2, "cost 2"), "STOP")])
    stats = extract_document(conn, doc["id"], resumed, cache, cfg)
    assert stats["requests"] == 2 and stats["cached"] == 1
    rows = db.rows(conn, "select page_no, metric_raw from claims where doc_id = ? order by id", (doc["id"],))
    assert rows == [{"page_no": 1, "metric_raw": "revenue"}, {"page_no": 2, "metric_raw": "cost"}]
    assert "Metric registry so far: revenue, unrelated_key" in resumed.prompts[0]
    counts = {r["key"]: r["claim_count"] for r in db.rows(conn, "select key, claim_count from metrics")}
    assert counts == {"unrelated_key": 1, "revenue": 1, "cost": 1}


def test_reextract_keeps_metrics_referenced_by_other_documents(tmp_path):
    from strata.canon import canonicalise_document
    from strata.verify import verify_document

    cfg = dataclasses.replace(config.load({}), pages_per_request=50)
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    cache = Cache(tmp_path / "cache", enabled=True)
    first = ingest_bytes(conn, make_pdf(["revenue 1 " * 10]), "a.pdf")
    extract_document(conn, first["id"], ScriptedProvider([(claim(1, "revenue", 1, "revenue 1"), "STOP")]), cache, cfg)
    verify_document(conn, first["id"])
    canonicalise_document(conn, first["id"])
    second = ingest_bytes(conn, make_pdf(["revenue 2 " * 10]), "b.pdf")
    extract_document(conn, second["id"], ScriptedProvider([(claim(1, "revenue", 2, "revenue 2"), "STOP")]), cache, cfg)
    stats = extract_document(conn, second["id"], ScriptedProvider([]), cache, cfg)
    assert stats["cached"] == 1
    assert db.one(conn, "select claim_count as n from metrics where key = 'revenue'")["n"] == 2
    assert db.one(conn, "select count(*) as n from claims")["n"] == 2


def test_extract_document_replays_from_cache(tmp_path):
    cfg = dataclasses.replace(config.load({}), pages_per_request=50)
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    pdf = make_pdf([" ".join(["text"] * 50)])
    doc = ingest_bytes(conn, pdf, "one.pdf")
    cache = Cache(tmp_path / "cache", enabled=True)
    live = ScriptedProvider([(claim(1, "revenue", 1, "revenue 1"), "STOP")])
    extract_document(conn, doc["id"], live, cache, cfg)
    conn.execute("delete from claims")
    conn.execute("delete from metrics")
    replay = ScriptedProvider([])
    stats = extract_document(conn, doc["id"], replay, cache, cfg)
    assert stats["cached"] == 1 and stats["claims"] == 1 and replay.prompts == []
