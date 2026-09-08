import json

import pymupdf
import pytest

from strata import config, db
from strata.ingest import ingest_path
from strata.verify import char_span, grade, normalize, verify_document

STARTER = config.ROOT / "starter-datasets"
DECK = STARTER / "delhivery" / "03-delhivery-q4-fy24-earnings-presentation.pdf"
AR = STARTER / "delhivery" / "02-delhivery-annual-report-fy24-excerpt.pdf"
SURVEY = STARTER / "india-macroeconomy" / "01-india-economic-survey-2024-25-excerpt.pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("grew by 6.5 percent in FY2024/25", "grew by 6.5 percent in fy2024 25"),
        ("Sujan7, Non-Executive", "sujan, non-executive"),
        ("services(1,2) (₹ Cr)", "services ₹ cr"),
        ("FY24 Q4 H1 FY25", "fy24 q4 h1 fy25"),
        ("₹ 81,415.38 million", "₹ 81,415.38 million"),
        ("Rs. 1,23,456.78 crore", "rs. 1,23,456.78 crore"),
        ("ﬁnancial year", "financial year"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def page_text(path, page_no):
    return pymupdf.open(path)[page_no - 1].get_text()


@pytest.mark.parametrize(
    ("path", "page", "quote", "expected"),
    [
        (DECK, 6, "₹8,142 Cr FY24 revenue from services", ("exact", 0)),
        (DECK, 9, "Revenue from services FY24 8,142", ("tokens", 0)),
        (DECK, 9, "Revenue from services FY24 7,224", ("tokens", 0)),
        (DECK, 9, "Revenue from services FY24 8,412", (None, 0)),
        (
            AR,
            24,
            "Mr. Suvir Suren Sujan, Non-Executive Director (DIN: 01173669), resigned from the Board",
            ("exact", 0),
        ),
        (AR, 22, "revenue from operations on consolidated basis for FY24 stood at ₹ 81,415.38 million", ("exact", 0)),
        (AR, 23, "revenue from operations on consolidated basis for FY24 stood at ₹ 81,415.38 million", ("nearby", -1)),
        (SURVEY, 4, "India's real GDP is estimated to grow by 6.4 per cent in FY25", ("exact", 0)),
    ],
)
def test_grades_on_real_pages(path, page, quote, expected):
    doc = pymupdf.open(path)
    prev_text = doc[page - 2].get_text() if page > 1 else ""
    next_text = doc[page].get_text() if page < len(doc) else ""
    assert grade(quote, doc[page - 1].get_text(), prev_text, next_text) == expected


def test_tokens_grade_reaches_neighbouring_page():
    infographic = "740\nMn\nExpress parcels shipped\n1,429\nK tonnes"
    letter = "Letter to the Shareholders. Our commitment to driving long-term value creation."
    assert grade("740Mn Express parcels shipped", letter, infographic, "") == ("tokens", -1)
    assert grade("740Mn Express parcels shipped", letter, "", infographic) == ("tokens", 1)
    assert grade("999Mn Express parcels shipped", letter, infographic, "") == (None, 0)


def test_char_span_tolerates_line_breaks():
    text = "the revenue from operations on consolidated basis for \nFY24 stood at ₹ 81,415.38 million as against"
    span = char_span("revenue from operations on consolidated basis for FY24 stood at ₹ 81,415.38 million", text)
    assert span is not None
    assert text[span[0] : span[1]].startswith("revenue") and text[span[0] : span[1]].endswith("million")
    assert char_span("nothing like this", text) is None


def test_char_span_is_linear_on_repetitive_text():
    import time

    page = "1 1 1 1 1 " * 2000
    started = time.perf_counter()
    assert char_span("1 " * 29 + "2", page) is None
    assert char_span("1 1 1 1 1 1 1 1 1 1", page) == (0, 19)
    assert time.perf_counter() - started < 1.0
    assert char_span("Revenue FROM operations", "the revenue from\noperations rose") == (4, 27)
    assert char_span("", "anything") is None


def test_verify_document_on_the_deck(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "v.db"))
    doc = ingest_path(conn, DECK)
    rows = [
        (6, "₹8,142 Cr FY24 revenue from services"),
        (9, "Revenue from services FY24 8,142"),
        (9, "Revenue from services FY24 8,412"),
        (7, "₹8,142 Cr FY24 revenue from services"),
        (99, "anything"),
    ]
    conn.executemany(
        "insert into claims (doc_id, page_no, subject, metric_raw, value_raw, quote)"
        " values (?, ?, 'Delhivery', 'revenue', '8142', ?)",
        [(doc["id"], page, quote) for page, quote in rows],
    )
    conn.commit()
    stats = verify_document(conn, doc["id"])
    assert stats == {
        "exact": 1,
        "nearby": 1,
        "tokens": 1,
        "quarantine": 2,
        "reasons": {"quote_not_found": 1, "page_out_of_range": 1},
    }
    evidence = db.rows(conn, "select claim_id, page_no, grade, bbox_json, char_start from evidence order by claim_id")
    assert [(e["page_no"], e["grade"]) for e in evidence] == [(6, "exact"), (9, "tokens"), (6, "nearby")]
    assert json.loads(evidence[0]["bbox_json"]) and evidence[0]["char_start"] is not None
    assert db.one(conn, "select page_no, status from claims where id = 4") == {"page_no": 6, "status": "verified"}
    assert db.one(conn, "select status from claims where id = 5") == {"status": "quarantined"}
