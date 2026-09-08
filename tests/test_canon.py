import json

import pytest

from strata import db
from strata.canon import (
    basis_marker,
    canonicalise_document,
    entity_key,
    parse_period,
    parse_unit,
    parse_value,
    published_key,
    resolve_metric,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FY24", ("2023-04-01", "2024-03-31")),
        ("FY 2024", ("2023-04-01", "2024-03-31")),
        ("FY2023-24", ("2023-04-01", "2024-03-31")),
        ("FY2024/25", ("2024-04-01", "2025-03-31")),
        ("fy 2023–24", ("2023-04-01", "2024-03-31")),
        ("2024-25", ("2024-04-01", "2025-03-31")),
        ("2024-25 (P)", ("2024-04-01", "2025-03-31")),
        ("2025-26", ("2025-04-01", "2026-03-31")),
        ("Q4 FY24", ("2024-01-01", "2024-03-31")),
        ("Q4FY24", ("2024-01-01", "2024-03-31")),
        ("Q1 FY25", ("2024-04-01", "2024-06-30")),
        ("Q3 FY2024-25", ("2024-10-01", "2024-12-31")),
        ("H1 FY25", ("2024-04-01", "2024-09-30")),
        ("year ended March 31, 2024", ("2023-04-01", "2024-03-31")),
        ("for the year ended 31 March 2024", ("2023-04-01", "2024-03-31")),
        ("quarter ended March 31, 2024", ("2024-01-01", "2024-03-31")),
        ("as at March 31, 2024", ("2024-03-31", "2024-03-31")),
        ("as on 31st March, 2024", ("2024-03-31", "2024-03-31")),
        ("March 31, 2024", ("2024-03-31", "2024-03-31")),
        ("2024-03-31", ("2024-03-31", "2024-03-31")),
        ("31.03.2024", ("2024-03-31", "2024-03-31")),
        ("Mar '24", ("2024-03-01", "2024-03-31")),
        ("March 2024", ("2024-03-01", "2024-03-31")),
        ("2024", ("2024-01-01", "2024-12-31")),
        ("CY2024", ("2024-01-01", "2024-12-31")),
        ("2024Q2", ("2024-04-01", "2024-06-30")),
        ("2023-08-24", ("2023-08-24", "2023-08-24")),
        ("nine months period ended December 31, 2021", ("2021-04-01", "2021-12-31")),
        ("six months ended September 30, 2024", ("2024-04-01", "2024-09-30")),
        ("April-December 2024", ("2024-04-01", "2024-12-31")),
        ("April – December 2024", ("2024-04-01", "2024-12-31")),
        ("April to November 2024", ("2024-04-01", "2024-11-30")),
        ("October 2024 to March 2025", ("2024-10-01", "2025-03-31")),
        ("first nine months of FY25", ("2024-04-01", "2024-12-31")),
        ("first eight months of FY25 (up to 6 January 2025)", ("2024-04-01", "2024-11-30")),
        ("H1 of FY25", ("2024-04-01", "2024-09-30")),
        ("Q2 of FY25", ("2024-07-01", "2024-09-30")),
        ("Q3 of 2024", ("2024-07-01", "2024-09-30")),
        ("FY25 (April-December)", ("2024-04-01", "2024-12-31")),
        ("end-FY2024/25", ("2025-03-31", "2025-03-31")),
        ("as at end-March 2025", ("2025-03-31", "2025-03-31")),
        ("end of September 2024", ("2024-09-30", "2024-09-30")),
        ("FY25*", ("2024-04-01", "2025-03-31")),
        ("date of this Prospectus", None),
        ("since inception", None),
        ("", None),
        ("null", None),
        ("the medium term", None),
    ],
)
def test_parse_period(raw, expected):
    assert parse_period(raw) == expected


def test_basis_marker():
    assert basis_marker("2024-25 (P)") == "provisional"
    assert basis_marker("2025-26 (BE)") == "budget"
    assert basis_marker("2024-25 (FAE)") == "advance_estimate"
    assert basis_marker("FY24") is None


def test_basis_phrase_from_quote():
    from strata.canon import basis_phrase, canonicalise_claim

    assert (
        basis_phrase("As per the first advance estimates, real GDP is estimated to grow by 6.4 per cent")
        == "advance_estimate"
    )
    assert basis_phrase("real GDP growth for 2025-26 is projected at 6.5 per cent") == "projection"
    assert basis_phrase("growth moderated to 6.5 per cent in 2024-25") is None
    stated = {
        "id": 1,
        "doc_id": 1,
        "subject": "India",
        "metric_raw": "gdp_growth",
        "value_raw": "6.4",
        "unit_raw": "per cent",
        "period_raw": "FY25",
        "basis_raw": "actual",
        "quote": "As per the first advance estimates, 6.4 per cent",
        "keys_json": "{}",
    }
    conn = db.init(db.connect(path=tmp_path_for_basis()))
    make_doc(conn, "s.pdf", "MoF", "2025-01")
    assert canonicalise_claim(conn, stated)["basis_canon"] == "advance_estimate"
    stated["basis_raw"] = "provisional"
    assert canonicalise_claim(conn, stated)["basis_canon"] == "provisional"


def tmp_path_for_basis():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp()) / "b.db"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INR million", ("INR", 1e6)),
        ("₹ Cr", ("INR", 1e7)),
        ("INR crore", ("INR", 1e7)),
        ("Rs. lakh", ("INR", 1e5)),
        ("Indian Rupees in million", ("INR", 1e6)),
        ("USD billion", ("USD", 1e9)),
        ("US$ million", ("USD", 1e6)),
        ("percent", ("pct", 1.0)),
        ("per cent", ("pct", 1.0)),
        ("%", ("pct", 1.0)),
        ("% of GDP", ("pct", 1.0)),
        ("percent of GDP", ("pct", 1.0)),
        ("bps", ("pct", 0.01)),
        ("count", ("count", 1.0)),
        ("million", ("count", 1e6)),
        ("thousand tons", ("tons", 1e3)),
        ("million sq ft", ("sq ft", 1e6)),
        ("days", ("days", 1.0)),
        ("x", ("x", 1.0)),
        (None, None),
        ("null", None),
    ],
)
def test_parse_unit(raw, expected):
    assert parse_unit(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("81415.38", 81415.38),
        ("1,23,456.78", 123456.78),
        ("(48.12)", -48.12),
        ("-1008", -1008.0),
        ("₹ 81,415.38", 81415.38),
        ("12.68%", 12.68),
        ("Rs. 578", 578.0),
        ("–", None),
        ("resigned", None),
        (None, None),
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Delhivery Limited", "delhivery"),
        ("Delhivery Ltd.", "delhivery"),
        ("Mr. Suvir Suren Sujan", "suvir suren sujan"),
        ("Government of India", "government of india"),
        ("Reserve Bank of India", "reserve bank of india"),
        ("India", "india"),
        ("Delhivery Limited - Express Parcel", "delhivery limited express parcel"),
    ],
)
def test_entity_key(raw, expected):
    assert entity_key(raw) == expected


def test_published_key():
    assert published_key("2024-05-17") == "2024-05-17"
    assert published_key("2024-07") == "2024-07-01"
    assert published_key("2025") == "2025-01-01"
    assert published_key("May 2025") == "2025-05-01"
    assert published_key("January 31, 2025") == "2025-01-31"
    assert published_key(None) is None


def seed(conn, doc_id, claims):
    for c in claims:
        conn.execute(
            "insert into claims (doc_id, page_no, subject, metric_raw, label, value_raw, unit_raw, period_raw,"
            " scope_raw, basis_raw, quote, keys_json, status)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'q', ?, 'verified')",
            (
                doc_id,
                c.get("page", 1),
                c["subject"],
                c["metric"],
                c.get("label"),
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


def make_doc(conn, filename, publisher, published_at):
    conn.execute(
        "insert into documents (sha256, filename, publisher, published_at, page_count, status)"
        " values (?, ?, ?, ?, 1, 'extracted')",
        (filename, filename, publisher, published_at),
    )
    return db.one(conn, "select id from documents where sha256 = ?", (filename,))["id"]


def test_canonicalise_document_builds_comparable_blocks(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "c.db"))
    ar = make_doc(conn, "ar.pdf", "Delhivery Limited", "2024-07")
    deck = make_doc(conn, "deck.pdf", "Delhivery Ltd.", "2024-05-17")
    seed(
        conn,
        ar,
        [
            {
                "subject": "Delhivery Limited",
                "metric": "revenue_from_operations",
                "value": "81415.38",
                "unit": "INR million",
                "period": "FY24",
                "scope": "consolidated",
            },
            {
                "subject": "Delhivery Limited",
                "metric": "revenue_from_operations",
                "value": "74540.82",
                "unit": "INR million",
                "period": "FY24",
                "scope": "standalone",
            },
            {
                "subject": "Mr. Suvir Suren Sujan",
                "metric": "board_role",
                "value": "resigned",
                "period": "2023-08-24",
                "keys": {"din": "01173669"},
            },
            {
                "subject": "Delhivery Limited",
                "metric": "employee_count",
                "value": "some text",
                "unit": None,
                "period": None,
            },
        ],
    )
    seed(
        conn,
        deck,
        [
            {
                "subject": "Delhivery Ltd.",
                "metric": "revenue_from_operation",
                "value": "8142",
                "unit": "INR crore",
                "period": "FY24",
            },
            {
                "subject": "Suvir Suren Sujan",
                "metric": "board_role",
                "value": "Non-Executive Director",
                "period": "2022-05-01",
                "keys": {"din": "01173669"},
            },
        ],
    )
    assert canonicalise_document(conn, ar) == {"numeric": 2, "text": 2, "non_comparable": 0}
    assert canonicalise_document(conn, deck) == {"numeric": 1, "text": 1, "non_comparable": 0}
    rows = db.rows(
        conn, "select c.doc_id, cc.* from claim_canon cc join claims c on c.id = cc.claim_id order by cc.claim_id"
    )
    consolidated, standalone, director_ar, _, deck_revenue, director_deck = rows
    assert consolidated["block_key"] == deck_revenue["block_key"]
    assert consolidated["block_key"] == standalone["block_key"]
    assert consolidated["metric_key"] == deck_revenue["metric_key"] == "revenue_from_operations"
    assert consolidated["value_canon"] == pytest.approx(81415.38e6)
    assert deck_revenue["value_canon"] == pytest.approx(8142e7)
    assert consolidated["period_start"] == "2023-04-01" and consolidated["period_end"] == "2024-03-31"
    assert consolidated["scope_canon"] == "consolidated" and deck_revenue["scope_canon"] is None
    assert director_ar["entity_id"] == director_deck["entity_id"]
    assert director_ar["block_key"] == director_deck["block_key"]
    assert director_ar["value_text"] == "resigned" and director_ar["period_end"] == "2023-08-24"
    entities = db.rows(conn, "select name_canon, kind, keys_json from entities order by id")
    assert entities[0]["name_canon"] == "delhivery"
    assert entities[1] == {"name_canon": "suvir suren sujan", "kind": "person", "keys_json": '{"din": "01173669"}'}
    metrics = {
        r["key"]: (r["claim_count"], json.loads(r["aliases_json"])) for r in db.rows(conn, "select * from metrics")
    }
    assert metrics["revenue_from_operations"] == (3, ["revenue_from_operation"])
    assert "revenue_from_operation" not in metrics


def test_resolve_metric_does_not_merge_distinct_measures(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "m.db"))
    doc = make_doc(conn, "m.pdf", "P", "2024")
    for key in ("net_profit", "ebitda", "revenue", "total_assets", "gdp_growth"):
        conn.execute("insert into metrics (key, claim_count) values (?, 1)", (key,))
    for key in ("cash_from_investing_activities", "cash_from_operating_activities"):
        conn.execute("insert into metrics (key, claim_count) values (?, 1)", (key,))
    assert resolve_metric(conn, "net_loss", None, doc) == "net_loss"
    assert resolve_metric(conn, "ebitda_margin", None, doc) == "ebitda_margin"
    assert resolve_metric(conn, "revenue_growth", None, doc) == "revenue_growth"
    assert resolve_metric(conn, "total_equity", None, doc) == "total_equity"
    assert resolve_metric(conn, "cash_from_financing_activities", None, doc) == "cash_from_financing_activities"
    assert resolve_metric(conn, "revenues", None, doc) == "revenue"
    assert resolve_metric(conn, "cash_from_operating_activity", None, doc) == "cash_from_operating_activities"


def test_precision_of():
    from strata.canon import precision_of

    assert precision_of("1.4") == 0.05
    assert precision_of("8142") == 0.5
    assert precision_of("81415.38") == 0.005
    assert precision_of("(48.12)") == 0.005
    assert precision_of("1,429") == 0.5
    assert precision_of("resigned") == 0.0
