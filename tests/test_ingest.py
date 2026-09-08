import json

import pymupdf
import pytest

from strata import config, db
from strata.ingest import detect_hints, ingest_bytes, ingest_path, summary


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("(All amounts in Indian Rupees in million, unless otherwise stated)", ["INR million"]),
        ("(Amount in ₹ crore)", ["INR crore"]),
        ("Revenue from services(1,2)\n(₹ Cr)\nFY24 performance", ["INR crore"]),
        ("(Amount in ₹ lakh)", ["INR lakh"]),
        ("(Rs. in lakhs)", ["INR lakh"]),
        ("(in ₹ million)", ["INR million"]),
        ("(In USD billion)", ["USD billion"]),
        ("(In percent of GDP)", ["percent"]),
        ("(in per cent)", ["percent"]),
        ("(%)", ["percent"]),
        ("(in million)", ["million"]),
        ("revenue grew to ₹715 million in FY21", []),
        ("₹8,142 Cr\nFY24 revenue from services", []),
        ("US$216 billion of freight", []),
    ],
)
def test_unit_declarations(text, units):
    assert detect_hints(text)["units"] == units


def test_scope_and_period():
    text = (
        "Standalone Balance Sheet as at March 31, 2024\n"
        "Consolidated Statement of Profit and Loss for the year ended March 31, 2024\n"
        "Notes as on 31st March, 2024"
    )
    hints = detect_hints(text)
    assert hints["scopes"] == ["standalone", "consolidated"]
    assert hints["periods"] == ["as at march 31, 2024", "for the year ended march 31, 2024", "as on 31st march, 2024"]
    assert hints["text_layer"] is True


def test_thin_pages_keep_number_heavy_slides():
    from strata.ingest import thin

    assert thin("Earnings Presentation\nQ4 & FY24", 5)
    assert thin("Appendix\n17", 2)
    assert thin("", 0)
    assert not thin("Revenue from services\n₹2,450 Cr\nQ2 FY26\nup 12% YoY", 9)
    assert not thin("Headcount\n58,000 employees\nas of September 30, 2025", 7)
    assert not thin("word " * 40, 40)


def test_no_text_layer():
    hints = detect_hints("")
    assert hints == {"units": [], "scopes": [], "periods": [], "text_layer": False}


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    return doc.tobytes()


def test_ingest_registers_pages(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    body = "\n".join(" ".join(["word"] * 10) for _ in range(6))
    pdf = make_pdf([body + "\n(Amount in Rs. crore)\nfor the year ended March 31, 2024", "", "short page"])
    result = ingest_bytes(conn, pdf, "sample.pdf")
    assert result["new"] is True
    assert result["page_count"] == 3
    assert result["skipped"] == 2
    rows = db.rows(
        conn,
        "select page_no, word_count, hints_json, skipped from pages where doc_id = ? order by page_no",
        (result["id"],),
    )
    first, blank, short = (json.loads(r["hints_json"]) for r in rows)
    assert first["units"] == ["INR crore"]
    assert first["periods"] == ["for the year ended march 31, 2024"]
    assert rows[0]["skipped"] == 0
    assert blank["text_layer"] is False and rows[1]["skipped"] == 1
    assert short["text_layer"] is True and rows[2]["skipped"] == 1
    assert db.one(conn, "select length(pdf) as n from documents where id = ?", (result["id"],))["n"] == len(pdf)
    assert summary(conn, result["id"]) == {
        "pages": 3,
        "skipped": 2,
        "no_text_layer": 1,
        "unit_pages": 1,
        "scope_pages": 0,
        "period_pages": 1,
    }


def test_ingest_survives_a_duplicate_insert_race(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "race.db"))
    pdf = make_pdf([" ".join(["x"] * 50)])
    first = ingest_bytes(conn, pdf, "a.pdf")
    from strata import ingest as ingest_module

    real_one = ingest_module.db.one
    calls = {"n": 0}

    def racing_one(c, sql, params=()):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_one(c, sql, params)

    ingest_module.db.one = racing_one
    try:
        again = ingest_bytes(conn, pdf, "a.pdf")
    finally:
        ingest_module.db.one = real_one
    assert again["new"] is False and again["id"] == first["id"]
    assert db.one(conn, "select count(*) as n from documents")["n"] == 1


def test_ingest_tolerates_a_broken_page(tmp_path, monkeypatch):
    conn = db.init(db.connect(path=tmp_path / "broken.db"))
    real = pymupdf.Page.get_text

    def flaky(self, *args, **kwargs):
        if self.number == 1:
            raise RuntimeError("cannot parse content stream")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", flaky)
    result = ingest_bytes(conn, make_pdf(["word " * 50, "word " * 50, "word " * 50]), "flaky.pdf")
    rows = db.rows(conn, "select page_no, word_count, skipped from pages order by page_no")
    counts = [r["word_count"] for r in rows]
    assert result["page_count"] == 3 and counts[0] > 0 and counts[1] == 0 and counts[2] == counts[0]
    assert rows[1]["skipped"] == 1


def test_ingest_is_idempotent(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    pdf = make_pdf([" ".join(["x"] * 50)])
    first = ingest_bytes(conn, pdf, "a.pdf")
    again = ingest_bytes(conn, pdf, "renamed.pdf")
    assert again["new"] is False and again["id"] == first["id"]
    assert db.one(conn, "select count(*) as n from documents")["n"] == 1
    assert db.one(conn, "select count(*) as n from pages")["n"] == 1


def test_ingest_on_libsql(tmp_path):
    import libsql

    conn = db.init(libsql.connect(str(tmp_path / "l.db")))
    pdf = make_pdf([" ".join(["x"] * 50)])
    result = ingest_bytes(conn, pdf, "l.pdf")
    assert result["page_count"] == 1
    assert db.one(conn, "select length(pdf) as n from documents")["n"] == len(pdf)


def test_ingest_starter_deck(tmp_path):
    path = config.ROOT / "starter-datasets" / "delhivery" / "03-delhivery-q4-fy24-earnings-presentation.pdf"
    conn = db.init(db.connect(path=tmp_path / "d.db"))
    result = ingest_path(conn, path)
    assert result["page_count"] == 27
    assert result["skipped"] == 4
    units = {
        u
        for row in db.rows(conn, "select hints_json from pages where doc_id = ?", (result["id"],))
        for u in json.loads(row["hints_json"])["units"]
    }
    assert "INR crore" in units
