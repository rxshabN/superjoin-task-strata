import dataclasses
import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

from strata import config, db
from strata.canon import canonicalise_document
from strata.ingest import ingest_bytes
from strata.providers.base import Response
from strata.reconcile import reconcile
from strata.verify import verify_document


def make_pdf(pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    return doc.tobytes()


def seed_claim(conn, doc_id, page, subject, metric, value, unit, period, quote, basis="actual", label=None, scope=None):
    conn.execute(
        "insert into claims (doc_id, page_no, subject, metric_raw, label, value_raw, unit_raw, period_raw, scope_raw,"
        " basis_raw, quote, keys_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
        (doc_id, page, subject, metric, label or metric, value, unit, period, scope, basis, quote),
    )
    conn.execute(
        "insert into metrics (key, claim_count) values (?, 1)"
        " on conflict(key) do update set claim_count = claim_count + 1",
        (metric,),
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = dataclasses.replace(
        config.settings, db="sqlite", db_path=tmp_path / "api.db", cache_dir=tmp_path / "cache", max_upload_pages=3
    )
    monkeypatch.setattr(config, "settings", settings)
    conn = db.init(db.connect(settings))
    survey = ingest_bytes(
        conn, make_pdf(["India's real GDP is estimated to grow by 6.4 per cent in FY25 " * 4]), "survey.pdf"
    )
    imf = ingest_bytes(
        conn, make_pdf(["India's real GDP grew by 6.5 percent in FY2024/25 " * 4, "second page"]), "imf.pdf"
    )
    conn.execute(
        "update documents set publisher = 'Ministry of Finance', published_at = '2025-01', status = 'extracted'"
        " where id = ?",
        (survey["id"],),
    )
    conn.execute(
        "update documents set publisher = 'International Monetary Fund', published_at = '2025-11',"
        " status = 'extracted' where id = ?",
        (imf["id"],),
    )
    seed_claim(
        conn,
        survey["id"],
        1,
        "India",
        "gdp_growth",
        "6.4",
        "per cent",
        "FY25",
        "estimated to grow by 6.4 per cent in FY25",
        basis="advance_estimate",
    )
    seed_claim(
        conn,
        imf["id"],
        1,
        "India",
        "gdp_growth",
        "6.5",
        "percent",
        "FY2024/25",
        "real GDP grew by 6.5 percent in FY2024/25",
    )
    seed_claim(
        conn, imf["id"], 2, "India", "gdp_growth", "9.9", "percent", "FY2024/25", "this quote is not on the page"
    )
    conn.commit()
    for doc_id in (survey["id"], imf["id"]):
        verify_document(conn, doc_id)
        canonicalise_document(conn, doc_id)
    reconcile(conn)
    conn.close()
    with TestClient(__import__("strata.api", fromlist=["app"]).app) as c:
        c.survey, c.imf = survey["id"], imf["id"]
        yield c


def test_documents_and_counts(client):
    data = client.get("/documents").json()
    assert [d["filename"] for d in data["documents"]] == ["survey.pdf", "imf.pdf"]
    imf = data["documents"][1]
    assert imf["claims"] == 2 and imf["verified"] == 1 and imf["quarantined"] == 1 and imf["exact"] == 1
    assert data["relations"]["cross"] == {"supersedes": 1}
    assert client.get(f"/documents/{client.imf}").json()["publisher"] == "International Monetary Fund"
    assert client.get("/documents/99").status_code == 404


def test_claims_filters(client):
    listing = client.get("/claims").json()
    all_claims = listing["claims"]
    assert len(all_claims) == 3 and listing["total"] == 3
    page = client.get("/claims?limit=2&offset=2").json()
    assert len(page["claims"]) == 1 and page["total"] == 3 and page["offset"] == 2
    verified = client.get("/claims?status=verified&metric=gdp_growth").json()["claims"]
    assert [c["value_raw"] for c in verified] == ["6.4", "6.5"]
    assert verified[0]["entity"] == "india" and verified[0]["period_start"] == "2024-04-01"
    assert verified[0]["basis_canon"] == "advance_estimate" and verified[0]["grade"] == "exact"
    assert client.get("/claims?entity=india&grade=exact").json()["claims"][0]["filename"] == "survey.pdf"
    assert client.get("/claims?q=grew").json()["claims"][0]["filename"] == "imf.pdf"
    one = client.get(f"/claims/{verified[0]['id']}").json()
    assert one["bboxes"] and one["keys"] == {}
    assert client.get("/claims/999").status_code == 404


def test_relations_and_detail(client):
    listing = client.get("/relations").json()
    rels = listing["relations"]
    assert len(rels) == 1 and listing["total"] == 1
    assert rels[0]["kind"] == "supersedes" and rels[0]["dimension"] == "basis"
    assert client.get("/relations?limit=1&offset=1").json()["relations"] == []
    assert rels[0]["a"]["value_raw"] == "6.5" and rels[0]["b"]["value_raw"] == "6.4"
    assert client.get("/relations?kind=contradicts").json()["relations"] == []
    detail = client.get(f"/relations/{rels[0]['id']}").json()
    assert detail["a"]["filename"] == "imf.pdf" and "basis matured" in detail["explanation"]
    assert client.get("/relations/999").status_code == 404


def test_quarantine_metrics_entities(client):
    q = client.get("/quarantine").json()["claims"]
    assert len(q) == 1 and q[0]["quarantine_reason"] == "quote_not_found" and q[0]["value_raw"] == "9.9"
    metrics = client.get("/metrics").json()["metrics"]
    assert metrics[0]["key"] == "gdp_growth" and metrics[0]["claim_count"] == 3
    entities = client.get("/entities").json()["entities"]
    assert entities[0]["name_canon"] == "india" and entities[0]["claims"] == 2


def test_answer(client):
    data = client.get("/answer?entity=India&metric=gdp_growth&period=FY25").json()
    assert data["found"] and data["current"]["value_raw"] == "6.5" and data["current"]["filename"] == "imf.pdf"
    assert data["history"][0]["claim"]["value_raw"] == "6.4" and "advance_estimate" in data["history"][0]["explanation"]
    assert data["members"] == 2
    assert client.get("/answer?entity=Nobody&metric=gdp_growth").json()["found"] is False
    assert client.get("/answer?entity=India&metric=gdp_growth&period=someday").json()["found"] is False
    assert client.get("/answer?entity=India&metric=gdp_growth&period=FY30").json()["found"] is False


def test_page_png(client):
    claim_id = client.get("/claims?status=verified").json()["claims"][0]["id"]
    r = client.get(f"/pages/{client.survey}/1.png?claim={claim_id}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert client.get(f"/pages/{client.survey}/9.png").status_code == 404
    assert client.get("/pages/99/1.png").status_code == 404


class ScriptedProvider:
    name = "scripted"
    model = "s-1"

    def generate(self, pdf_bytes, prompt):
        line = json.dumps(
            {
                "type": "claim",
                "page": 1,
                "subject": "Acme",
                "metric": "revenue",
                "value": 12,
                "unit": "INR crore",
                "period": "FY24",
                "quote": "revenue was 12 crore",
            }
        )
        return Response(text=line, finish_reason="STOP", output_tokens=10, model=self.model)


def test_upload_runs_pipeline(client, monkeypatch):
    from strata import pipeline

    monkeypatch.setattr(pipeline.providers, "get_provider", lambda *a, **k: ScriptedProvider())
    pdf = make_pdf(["Acme revenue was 12 crore in FY24 " * 5])
    r = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")})
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["new"] is True
    doc_id = r.json()["id"]
    doc = client.get(f"/documents/{doc_id}").json()
    assert doc["status"] == "ready" and doc["claims"] == 1 and doc["exact"] == 1
    again = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")}).json()
    assert again["new"] is False and again["status"] == "ready"
    assert client.get(f"/claims?doc={doc_id}").json()["claims"][0]["metric_key"] == "revenue"


def test_upload_rejections(client):
    post = lambda name, data: client.post("/documents", files={"file": (name, data, "application/pdf")})  # noqa: E731
    assert post("x.txt", b"hello").status_code == 400
    assert post("big.pdf", make_pdf(["p"] * 4)).status_code == 413
    assert post("broken.pdf", b"%PDF-1.7\n" + bytes(range(256)) * 8).status_code == 400
    locked = pymupdf.open()
    locked.new_page().insert_text((72, 72), "secret " * 50)
    encrypted = locked.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    r = post("locked.pdf", encrypted)
    assert r.status_code == 400 and "password" in r.json()["detail"]
    empty = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\n"
        b"endobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )
    r = post("empty.pdf", empty)
    assert r.status_code == 400 and "no pages" in r.json()["detail"]
    prefixed = post("prefixed.pdf", b"\r\n%junk\r\n" + make_pdf(["word " * 50]))
    assert prefixed.status_code == 202 and prefixed.json()["page_count"] == 1


def test_reupload_requeues_a_document_stuck_mid_pipeline(client, monkeypatch):
    from strata import pipeline

    monkeypatch.setattr(pipeline.providers, "get_provider", lambda *a, **k: ScriptedProvider())
    pdf = make_pdf(["Acme revenue was 12 crore in FY24 " * 5])
    doc_id = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")}).json()["id"]
    assert client.get(f"/documents/{doc_id}").json()["status"] == "ready"
    connection = __import__("strata.api", fromlist=["connection"]).connection
    for stuck in ("verifying", "reconciling", "failed"):
        with connection() as conn:
            conn.execute("update documents set status = ?, updated_at = null where id = ?", (stuck, doc_id))
            conn.commit()
        again = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")}).json()
        assert again["new"] is False and again["status"] == "queued"
        assert client.get(f"/documents/{doc_id}").json()["status"] == "ready"
    with connection() as conn:
        conn.execute(
            "update documents set status = 'extracting', updated_at = ? where id = ?", (pipeline.now(), doc_id)
        )
        conn.commit()
    busy = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")}).json()
    assert busy["new"] is False and busy["status"] == "extracting"
    assert client.get(f"/documents/{doc_id}").json()["status"] == "extracting"
    with connection() as conn:
        conn.execute("update documents set updated_at = '2020-01-01T00:00:00+00:00' where id = ?", (doc_id,))
        conn.commit()
    stale = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")}).json()
    assert stale["status"] == "queued" and client.get(f"/documents/{doc_id}").json()["status"] == "ready"


def test_worker_claims_a_document_once(client, monkeypatch):
    from strata import pipeline

    monkeypatch.setattr(pipeline.providers, "get_provider", lambda *a, **k: ScriptedProvider())
    pdf = make_pdf(["Acme revenue was 12 crore in FY24 " * 5])
    doc_id = client.post("/documents", files={"file": ("acme.pdf", pdf, "application/pdf")}).json()["id"]
    assert client.get(f"/documents/{doc_id}").json()["status"] == "ready"
    assert pipeline.process_document(doc_id) == {"status": "skipped"}
    assert client.get(f"/documents/{doc_id}").json()["claims"] == 1


def test_health_and_static(client):
    health = client.get("/health").json()
    assert health["ok"] is True and health["tables"] >= 9 and "upload_pages" in health["limits"]
    assert "Strata" in client.get("/").text
    assert client.get("/app.js").status_code == 200
