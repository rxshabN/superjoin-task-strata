import json

from strata import db
from strata.cache import Cache
from strata.canon import canonicalise_document, reset
from strata.consolidate import apply, build_prompt, consolidate, parse, registry_rows
from strata.providers import generate
from strata.providers.base import Response
from strata.reconcile import reconcile


class Scripted:
    name = "scripted"
    model = "s-1"

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def generate_text(self, prompt):
        self.calls += 1
        return Response(text=self.text, finish_reason="STOP", output_tokens=3, model=self.model)


def make_doc(conn, filename, publisher, published_at):
    conn.execute(
        "insert into documents (sha256, filename, publisher, published_at, page_count, status)"
        " values (?, ?, ?, ?, 1, 'extracted')",
        (filename, filename, publisher, published_at),
    )
    return db.one(conn, "select id from documents where sha256 = ?", (filename,))["id"]


def add_claim(conn, doc_id, subject, metric, value, unit, period, label=None, scope=None):
    conn.execute(
        "insert into claims (doc_id, page_no, subject, metric_raw, label, value_raw, unit_raw, period_raw, scope_raw,"
        " basis_raw, quote, keys_json, status) values (?, 1, ?, ?, ?, ?, ?, ?, ?, 'actual', ?, '{}', 'verified')",
        (doc_id, subject, metric, label or metric, value, unit, period, scope, f"{label or metric} {value}"),
    )
    conn.execute(
        "insert into metrics (key, label, first_seen_doc, claim_count) values (?, ?, ?, 1)"
        " on conflict(key) do update set claim_count = claim_count + 1",
        (metric, label or metric, doc_id),
    )


def test_parse_ignores_noise():
    text = "\n".join(
        [
            "```",
            '{"keep":"revenue","merge":["revenues","turnover"],"why":"same measure"}',
            "not json",
            '{"keep":"ebitda","merge":[]}',
            '{"merge":["x"]}',
            '{"keep":"gdp_growth","merge":["real_gdp_growth", 7]}',
            "```",
        ]
    )
    assert parse(text) == [
        {"keep": "revenue", "merge": ["revenues", "turnover"], "why": "same measure"},
        {"keep": "gdp_growth", "merge": ["real_gdp_growth"], "why": ""},
    ]


def test_apply_validates_groups(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "a.db"))
    doc = make_doc(conn, "a.pdf", "P", "2024")
    add_claim(conn, doc, "Acme", "revenue_from_operations", "10", "INR crore", "FY24")
    add_claim(conn, doc, "Acme", "revenue_from_operations", "11", "INR crore", "FY23")
    add_claim(conn, doc, "Acme", "revenue_from_contracts_with_customers", "10", "INR crore", "FY24")
    add_claim(conn, doc, "Acme", "revenue_growth", "12", "percent", "FY24")
    add_claim(conn, doc, "Acme", "ebitda", "3", "INR crore", "FY24")
    conn.commit()
    canonicalise_document(conn, doc)
    stats = apply(
        conn,
        [
            {
                "keep": "revenue_from_operations",
                "merge": ["revenue_from_contracts_with_customers", "revenue_growth", "nope"],
                "why": "one revenue",
            },
            {"keep": "revenue_from_contracts_with_customers", "merge": ["ebitda"], "why": "chain"},
        ],
    )
    assert stats["applied"] == [("revenue_from_contracts_with_customers", "revenue_from_operations")]
    assert ("revenue_growth", "revenue_from_operations", "unit differs (pct vs INR)") in stats["rejected"]
    assert ("nope", "revenue_from_operations", "unknown key") in stats["rejected"]
    assert ("revenue_from_contracts_with_customers", None, "keep is merged away in another group") in stats["rejected"]
    rows = db.rows(conn, "select alias, key, source from metric_aliases")
    assert rows == [
        {"alias": "revenue_from_contracts_with_customers", "key": "revenue_from_operations", "source": "one revenue"}
    ]


def test_build_prompt_lists_keys_with_units_and_labels(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "p.db"))
    doc = make_doc(conn, "p.pdf", "P", "2024")
    add_claim(conn, doc, "Acme", "revenue", "10", "INR crore", "FY24", label="Revenue from operations")
    add_claim(conn, doc, "Acme", "revenue", "9", "INR crore", "FY23", label="Turnover")
    add_claim(conn, doc, "India", "gdp_growth", "6.5", "percent", "FY25", label="Real GDP growth")
    conn.commit()
    canonicalise_document(conn, doc)
    rows = registry_rows(conn)
    assert rows == [
        {"key": "gdp_growth", "claims": 1, "unit": "pct", "labels": ["Real GDP growth"]},
        {"key": "revenue", "claims": 2, "unit": "INR", "labels": ["Revenue from operations", "Turnover"]},
    ]
    prompt = build_prompt(rows)
    assert "revenue | INR | 2 | Revenue from operations ; Turnover" in prompt
    assert prompt.count("\n{") == 1 and "Return nothing at all" in prompt


def test_consolidation_merges_blocks_and_replays_from_cache(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "c.db"))
    report = make_doc(conn, "report.pdf", "Delhivery Limited", "2024-07")
    deck = make_doc(conn, "deck.pdf", "Delhivery Limited", "2024-05")
    add_claim(
        conn,
        report,
        "Delhivery Limited",
        "revenue_from_operations",
        "81415.38",
        "INR million",
        "FY24",
        scope="consolidated",
    )
    add_claim(conn, deck, "Delhivery Limited", "revenue_from_contracts_with_customers", "8142", "INR crore", "FY24")
    conn.commit()
    for doc in (report, deck):
        canonicalise_document(conn, doc)
    assert reconcile(conn)["pairs"] == 0
    cache = Cache(tmp_path / "cache", enabled=True)
    live = Scripted(
        '{"keep":"revenue_from_operations","merge":["revenue_from_contracts_with_customers"],"why":"same line"}'
    )
    stats = consolidate(conn, live, cache)
    assert stats["applied"] == [("revenue_from_contracts_with_customers", "revenue_from_operations")]
    assert stats["cached"] is False and stats["keys"] == 2
    replay = consolidate(conn, Scripted("should not be called"), cache)
    assert replay["cached"] is True and live.calls == 1
    assert replay["applied"] == [] and replay["rejected"][0][2] == "already used"
    reset(conn)
    for doc in (report, deck):
        canonicalise_document(conn, doc)
    blocks = {r["block_key"] for r in db.rows(conn, "select block_key from claim_canon")}
    assert len(blocks) == 1 and "|revenue_from_operations|" in next(iter(blocks))
    assert reconcile(conn)["corroborates"] == 1
    metrics = {r["key"]: json.loads(r["aliases_json"]) for r in db.rows(conn, "select key, aliases_json from metrics")}
    assert metrics == {"revenue_from_operations": ["revenue_from_contracts_with_customers"]}


def test_generate_without_pdf_uses_generate_text_and_caches(tmp_path):
    cache = Cache(tmp_path / "cache", enabled=True)
    provider = Scripted("hello")
    first = generate(None, "prompt", provider, cache)
    second = generate(None, "prompt", provider, cache)
    assert first.text == "hello" and first.cached is False
    assert second.cached is True and provider.calls == 1


def test_merge_rewrites_block_keys_of_earlier_rows(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "k.db"))
    first = make_doc(conn, "first.pdf", "P", "2023")
    second = make_doc(conn, "second.pdf", "P", "2024")
    add_claim(conn, first, "Acme", "revenues", "9", "INR crore", "FY23")
    add_claim(conn, first, "Ann", "board_roles", "director", None, "2023-01-01")
    add_claim(conn, second, "Acme", "revenue", "10", "INR crore", "FY23")
    add_claim(conn, second, "Acme", "revenue", "11", "INR crore", "FY24")
    add_claim(conn, second, "Ann", "board_role", "resigned", None, "2024-01-01")
    add_claim(conn, second, "Ann", "board_role", "chair", None, "2024-06-01")
    conn.commit()
    canonicalise_document(conn, first)
    canonicalise_document(conn, second)
    rows = db.rows(conn, "select metric_key, block_key from claim_canon order by claim_id")
    assert {r["metric_key"] for r in rows} == {"revenue", "board_role"}
    assert rows[0]["block_key"] == rows[2]["block_key"]
    assert rows[1]["block_key"] == rows[4]["block_key"] == rows[5]["block_key"]
    assert rows[1]["block_key"].endswith("|board_role")
