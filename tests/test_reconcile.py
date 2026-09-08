import json

from strata import db
from strata.canon import canonicalise_document
from strata.reconcile import blocks_for_document, close, label_tokens, reconcile, relate


def claim(
    id,
    value,
    unit="INR",
    scope=None,
    basis="actual",
    publisher="A",
    published_at="2024-01-01",
    text=None,
    period_end=None,
    label=None,
):
    return {
        "id": id,
        "value_canon": value,
        "value_text": text,
        "unit_canon": unit,
        "scope_canon": scope,
        "basis_canon": basis,
        "publisher": publisher,
        "published_at": published_at,
        "period_end": period_end,
        "label": label,
    }


def test_label_tokens_ignore_periods_and_noise():
    assert label_tokens("FY24 EBITDA") == label_tokens("EBITDA")
    assert label_tokens("Total Revenue") == label_tokens("Revenue")
    assert label_tokens("Revenue for the year ended March 31, 2024") == label_tokens("Revenue")
    assert label_tokens("Adjusted EBITDA") != label_tokens("EBITDA")
    assert label_tokens("Revenue from Operations") != label_tokens("Revenue from services")
    assert label_tokens(None) == frozenset()


def test_same_publisher_different_labels_are_line_items():
    total = claim(1, 81415.38e6, publisher="Delhivery", published_at="2024-07", label="Revenue from Operations")
    segment = claim(
        2, 6087.96e6, publisher="Delhivery", published_at="2024-07", label="Revenues from truckload services"
    )
    rel = relate(total, segment)
    assert rel["kind"] == "reconciled" and rel["dimension"] == "label" and rel["confidence"] == "low"
    ebitda = claim(3, 127e7, publisher="Delhivery", published_at="2024-05-17", label="FY24 EBITDA")
    adjusted = claim(4, 76e7, publisher="Delhivery", published_at="2024-05-17", label="Adjusted EBITDA")
    assert relate(ebitda, adjusted)["kind"] == "reconciled"
    same_label = claim(5, 130e7, publisher="Delhivery", published_at="2024-05-17", label="EBITDA")
    assert relate(ebitda, same_label)["kind"] == "contradicts"


def test_different_publishers_with_different_labels_still_contradict():
    rel = relate(
        claim(1, 4.8, unit="pct", publisher="IMF", published_at="2025-11", label="Fiscal deficit"),
        claim(
            2, 4.4, unit="pct", publisher="Ministry of Finance", published_at="2025-01", label="Gross fiscal deficit"
        ),
    )
    assert rel["kind"] == "contradicts" and rel["confidence"] == "low" and "labels differ" in rel["explanation"]


def test_close_uses_printed_precision():
    assert close(8.141538e10, 8.142e10, 5e6)
    assert not close(7.454082e10, 8.141538e10, 5e6)
    assert close(12.68, 12.7, 0.05)
    assert not close(6.4, 6.5, 0.05)
    assert close(1.4e6, 1.429e6, 5e4)
    assert not close(1.4e6, 1.429e6, 500)
    assert close(0, 0, 0.5)


def test_close_fallback_without_precision():
    assert close(8.141538e10, 8.142e10)
    assert not close(6.4, 6.5)
    assert close(12.68, 12.7)


def test_case_one_units_differ_values_agree():
    rel = relate(
        claim(1, 81415.38e6, publisher="Delhivery", published_at="2024-07"),
        claim(2, 8142e7, publisher="Delhivery Ltd", published_at="2024-05-17"),
    )
    assert rel["kind"] == "corroborates" and rel["explanation"] is None


def test_agreement_first_notes_differing_scope():
    rel = relate(claim(1, 8142e7, scope="excluding traded goods"), claim(2, 8142e7, scope="including traded goods"))
    assert rel["kind"] == "corroborates" and "scope differs" in rel["explanation"]


def test_scope_split_reconciles():
    rel = relate(claim(1, 74540.82e6, scope="standalone"), claim(2, 81415.38e6, scope="consolidated"))
    assert rel == {
        "kind": "reconciled",
        "a_id": 1,
        "b_id": 2,
        "dimension": "scope",
        "explanation": "standalone vs consolidated",
        "confidence": "high",
    }


def test_scope_unstated_never_contradicts():
    rel = relate(claim(1, 74540.82e6, scope="standalone"), claim(2, 81415.38e6, scope=None, publisher="B"))
    assert rel["kind"] == "reconciled" and rel["dimension"] == "scope" and rel["confidence"] == "low"


def test_basis_maturity_supersedes():
    survey = claim(
        1, 6.4, unit="pct", basis="advance_estimate", publisher="Ministry of Finance", published_at="2025-01"
    )
    rbi = claim(2, 6.5, unit="pct", basis="actual", publisher="Reserve Bank of India", published_at="2025-05")
    rel = relate(survey, rbi)
    assert rel["kind"] == "supersedes" and rel["a_id"] == 2 and rel["b_id"] == 1
    assert rel["explanation"] == "basis matured: advance_estimate → actual"


def test_projection_reconciles_not_supersedes():
    rel = relate(claim(1, 6.5, unit="pct", basis="projection"), claim(2, 6.8, unit="pct", basis="actual"))
    assert rel["kind"] == "reconciled" and rel["dimension"] == "basis"


def test_independent_sources_agree():
    rel = relate(
        claim(1, 6.5, unit="pct", publisher="RBI", published_at="2025-05"),
        claim(2, 6.5, unit="pct", publisher="IMF", published_at="2025-11"),
    )
    assert rel["kind"] == "corroborates"


def test_same_publisher_restates():
    old = claim(1, 72253.01e6, publisher="Delhivery Limited", published_at="2023-05-19")
    new = claim(2, 72236.47e6 * 0.99, publisher="Delhivery Ltd.", published_at="2024-07")
    rel = relate(old, new)
    assert rel["kind"] == "supersedes" and rel["a_id"] == 2 and rel["dimension"] == "vintage"


def test_different_publishers_contradict():
    rel = relate(
        claim(1, 4.8, unit="pct", publisher="IMF", published_at="2025-11"),
        claim(2, 4.4, unit="pct", publisher="Ministry of Finance", published_at="2025-01"),
    )
    assert rel["kind"] == "contradicts" and rel["confidence"] == "review"


def test_unknown_date_contradicts_with_reason():
    rel = relate(
        claim(1, 4.8, unit="pct", publisher="IMF", published_at=None),
        claim(2, 4.4, unit="pct", publisher="IMF", published_at=None),
    )
    assert rel["kind"] == "contradicts" and "publication date unknown" in rel["explanation"]


def test_unit_mismatch_yields_no_relation():
    assert relate(claim(1, 5, unit="INR"), claim(2, 5, unit="pct")) is None


def test_state_facts():
    active = claim(1, None, text="non-executive director", period_end="2022-05-01")
    resigned = claim(2, None, text="resigned", period_end="2023-08-24")
    rel = relate(active, resigned)
    assert rel["kind"] == "supersedes" and rel["a_id"] == 2 and rel["dimension"] == "time"
    same = relate(
        claim(1, None, text="director", period_end="2023-01-01"),
        claim(2, None, text="chairman", period_end="2023-01-01"),
    )
    assert same["kind"] == "contradicts"
    agree = relate(claim(1, None, text="director"), claim(2, None, text="director"))
    assert agree["kind"] == "corroborates"


def seed(conn, filename, publisher, published_at, claims):
    conn.execute(
        "insert into documents (sha256, filename, publisher, published_at, page_count, status)"
        " values (?, ?, ?, ?, 1, 'extracted')",
        (filename, filename, publisher, published_at),
    )
    doc_id = db.one(conn, "select id from documents where sha256 = ?", (filename,))["id"]
    for c in claims:
        conn.execute(
            "insert into claims (doc_id, page_no, subject, metric_raw, value_raw, unit_raw, period_raw, scope_raw,"
            " basis_raw, quote, keys_json, status)"
            " values (?, 1, ?, ?, ?, ?, ?, ?, ?, 'q', ?, 'verified')",
            (
                doc_id,
                c["subject"],
                c["metric"],
                c["value"],
                c.get("unit"),
                c.get("period"),
                c.get("scope"),
                c.get("basis", "actual"),
                json.dumps(c.get("keys", {})),
            ),
        )
        conn.execute(
            "insert into metrics (key, claim_count) values (?, 1)"
            " on conflict(key) do update set claim_count = claim_count + 1",
            (c["metric"],),
        )
    conn.commit()
    return doc_id


def test_reconcile_end_to_end(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "r.db"))
    survey = seed(
        conn,
        "survey.pdf",
        "Ministry of Finance",
        "2025-01",
        [
            {
                "subject": "India",
                "metric": "gdp_growth",
                "value": "6.4",
                "unit": "per cent",
                "period": "FY25",
                "basis": "advance_estimate",
            },
        ],
    )
    rbi = seed(
        conn,
        "rbi.pdf",
        "Reserve Bank of India",
        "2025-05",
        [
            {"subject": "India", "metric": "gdp_growth", "value": "6.5", "unit": "per cent", "period": "2024-25"},
            {
                "subject": "India",
                "metric": "gdp_growth",
                "value": "6.5",
                "unit": "per cent",
                "period": "2025-26",
                "basis": "projection",
            },
        ],
    )
    imf = seed(
        conn,
        "imf.pdf",
        "International Monetary Fund",
        "2025-11",
        [
            {"subject": "India", "metric": "gdp_growth", "value": "6.5", "unit": "percent", "period": "FY2024/25"},
            {
                "subject": "India",
                "metric": "gdp_growth",
                "value": "6.2",
                "unit": "percent",
                "period": "FY2025/26",
                "basis": "projection",
            },
        ],
    )
    for doc_id in (survey, rbi, imf):
        canonicalise_document(conn, doc_id)
    stats = reconcile(conn)
    assert stats == {"blocks": 2, "pairs": 4, "corroborates": 1, "reconciled": 0, "supersedes": 2, "contradicts": 1}
    rels = db.rows(
        conn,
        "select r.kind, r.dimension, a.value_raw as a, b.value_raw as b, r.explanation from relations r"
        " join claims a on a.id = r.a_id join claims b on b.id = r.b_id order by r.id",
    )
    kinds = {(r["kind"], r["a"], r["b"]) for r in rels}
    assert ("supersedes", "6.5", "6.4") in kinds
    assert ("corroborates", "6.5", "6.5") in kinds
    assert ("contradicts", "6.5", "6.2") in kinds
    assert blocks_for_document(conn, survey) < blocks_for_document(conn, rbi)
    again = reconcile(conn, blocks_for_document(conn, survey))
    assert again["pairs"] == 3 and db.one(conn, "select count(*) as n from relations")["n"] == 4


def test_different_publishers_never_wait_for_a_date():
    rel = relate(
        claim(1, 4.8, unit="pct", publisher="IMF", published_at=None),
        claim(2, 4.4, unit="pct", publisher="Ministry of Finance", published_at=None),
    )
    assert rel["kind"] == "contradicts" and rel["confidence"] == "review"
    assert rel["explanation"] == "every coordinate matches and the values differ"
    rel = relate(
        claim(1, 4.0, unit="pct", publisher="Reserve Bank of India", published_at=None, label="CPI inflation"),
        claim(2, 2.8, unit="pct", publisher="IMF", published_at="2025-11", label="Headline inflation"),
    )
    assert rel["kind"] == "contradicts" and rel["confidence"] == "low" and "labels differ" in rel["explanation"]
