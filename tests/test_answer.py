import dataclasses

import pytest

from strata import config, db, queries
from strata.canon import canonicalise_document
from strata.ingest import ingest_bytes
from strata.reconcile import reconcile
from strata.verify import verify_document
from tests.test_api import make_pdf, seed_claim


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = dataclasses.replace(config.settings, db="sqlite", db_path=tmp_path / "answer.db")
    monkeypatch.setattr(config, "settings", settings)
    return db.init(db.connect(settings))


def add_document(conn, filename, publisher, published_at, text):
    doc = ingest_bytes(conn, make_pdf([text * 4]), filename)
    conn.execute(
        "update documents set publisher = ?, published_at = ?, status = 'extracted' where id = ?",
        (publisher, published_at, doc["id"]),
    )
    return doc["id"]


def finish(conn, doc_ids):
    conn.commit()
    for doc_id in doc_ids:
        verify_document(conn, doc_id)
        canonicalise_document(conn, doc_id)
    reconcile(conn)


def test_history_lists_each_superseded_claim_once(conn):
    survey = add_document(conn, "survey.pdf", "Ministry of Finance", "2025-01", "growth of 6.4 per cent in FY25 ")
    rbi = add_document(conn, "rbi.pdf", "Reserve Bank of India", "2025-05", "growth of 6.5 per cent in 2024-25 ")
    imf = add_document(conn, "imf.pdf", "International Monetary Fund", "2025-11", "grew by 6.5 percent in FY2024/25 ")
    seed_claim(
        conn,
        survey,
        1,
        "India",
        "gdp_growth",
        "6.4",
        "per cent",
        "FY25",
        "growth of 6.4 per cent in FY25",
        "advance_estimate",
    )
    seed_claim(
        conn,
        rbi,
        1,
        "India",
        "gdp_growth",
        "6.5",
        "per cent",
        "2024-25",
        "growth of 6.5 per cent in 2024-25",
        "provisional",
    )
    seed_claim(conn, imf, 1, "India", "gdp_growth", "6.5", "percent", "FY2024/25", "grew by 6.5 percent in FY2024/25")
    finish(conn, [survey, rbi, imf])
    data = queries.answer(conn, "India", "gdp_growth", "FY25")
    assert data["found"] and data["members"] == 3
    assert data["current"]["filename"] == "imf.pdf" and data["current"]["basis_canon"] == "actual"
    assert [h["claim"]["value_raw"] for h in data["history"]] == ["6.4"]
    assert data["history"][0]["superseded_by"] == data["current"]["id"]
    assert [x["claim"]["filename"] for x in data["corroborated_by"]] == ["rbi.pdf"]


def test_current_is_the_corroborated_claim_not_the_last_line_item(conn):
    deck = add_document(conn, "deck.pdf", "Delhivery Limited", "2024-05-17", "Revenue for services 8,142 FY24 ")
    report = add_document(
        conn,
        "report.pdf",
        "Delhivery Limited",
        "2024-07-05",
        "Revenue from Operations 81,415.38 FY24. Revenue from Truck Load services 6,087.96 FY24 ",
    )
    seed_claim(
        conn,
        deck,
        1,
        "Delhivery Limited",
        "revenue",
        "8142",
        "INR crore",
        "FY24",
        "Revenue for services 8,142",
        label="Revenue for services",
    )
    seed_claim(
        conn,
        report,
        1,
        "Delhivery Limited",
        "revenue",
        "81415.38",
        "INR million",
        "FY24",
        "Revenue from Operations 81,415.38",
        label="Revenue from Operations",
        scope="consolidated",
    )
    seed_claim(
        conn,
        report,
        1,
        "Delhivery Limited",
        "revenue",
        "6087.96",
        "INR million",
        "FY24",
        "Revenue from Truck Load services 6,087.96",
        label="Revenue from Truck Load services",
        scope="consolidated",
    )
    finish(conn, [deck, report])
    data = queries.answer(conn, "Delhivery", "revenue", "FY24")
    assert data["found"] and data["members"] == 3
    assert data["current"]["value_raw"] == "81415.38" and data["current"]["filename"] == "report.pdf"
    assert [x["claim"]["value_raw"] for x in data["corroborated_by"]] == ["8142"]
    assert {x["dimension"] for x in data["reconciled_with"]} == {"label"}
    assert data["history"] == []
