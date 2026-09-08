import json
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pymupdf
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Response, UploadFile
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db, ingest, pipeline, queries

WEB = Path(__file__).resolve().parent.parent / "web"
LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = db.init(db.connect())
    yield
    close = getattr(app.state.db, "close", None)
    if close:
        close()


app = FastAPI(title="Strata", version=__version__, lifespan=lifespan)


@contextmanager
def connection():
    with LOCK:
        yield app.state.db


@app.get("/health")
def health():
    cfg = config.settings
    with connection() as conn:
        tables = len(db.tables(conn))
    return {
        "ok": True,
        "version": __version__,
        "provider": cfg.provider,
        "backend": cfg.gemini_backend if cfg.provider == "gemini" else None,
        "model": cfg.model,
        "db": db.backend(cfg),
        "tables": tables,
        "cache": cfg.cache_enabled,
        "limits": {"upload_mb": cfg.max_upload_mb, "upload_pages": cfg.max_upload_pages},
    }


@app.get("/documents")
def list_documents():
    with connection() as conn:
        return {"documents": queries.documents(conn), "relations": queries.relation_counts(conn)}


@app.post("/documents", status_code=202)
async def upload_document(
    file: UploadFile, background: BackgroundTasks, x_gemini_key: str | None = Header(default=None)
):
    cfg = config.settings
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "The file is not a PDF.")
    if len(data) > cfg.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"The file is larger than {cfg.max_upload_mb} MB.")
    try:
        page_count = len(pymupdf.open(stream=data, filetype="pdf"))
    except Exception as error:
        raise HTTPException(400, "The PDF could not be opened.") from error
    if page_count > cfg.max_upload_pages:
        raise HTTPException(413, f"The PDF has {page_count} pages; the limit is {cfg.max_upload_pages}.")
    with connection() as conn:
        result = ingest.ingest_bytes(conn, data, file.filename or "upload.pdf")
        status = db.one(conn, "select status from documents where id = ?", (result["id"],))["status"]
    if status in pipeline.PENDING:
        background.add_task(pipeline.process_document, result["id"], x_gemini_key)
        status = "queued"
    return {"id": result["id"], "status": status, "new": result["new"], "page_count": result["page_count"]}


@app.get("/documents/{doc_id}")
def get_document(doc_id: int):
    with connection() as conn:
        doc = queries.document(conn, doc_id)
    if not doc:
        raise HTTPException(404, "No such document.")
    return doc


@app.get("/claims")
def list_claims(
    doc: int | None = None,
    entity: str | None = None,
    metric: str | None = None,
    grade: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with connection() as conn:
        return {"claims": queries.claims(conn, doc, entity, metric, grade, status, q, limit, offset)}


@app.get("/claims/{claim_id}")
def get_claim(claim_id: int):
    with connection() as conn:
        found = queries.claim(conn, claim_id)
    if not found:
        raise HTTPException(404, "No such claim.")
    return found


@app.get("/relations")
def list_relations(
    kind: str | None = None,
    cross: bool = True,
    confidence: str | None = None,
    doc: int | None = None,
    metric: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with connection() as conn:
        return {"relations": queries.relations(conn, kind, cross, confidence, doc, metric, limit, offset)}


@app.get("/relations/{relation_id}")
def get_relation(relation_id: int):
    with connection() as conn:
        found = queries.relation(conn, relation_id)
    if not found:
        raise HTTPException(404, "No such relation.")
    return found


@app.get("/quarantine")
def list_quarantine(doc: int | None = None, limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
    with connection() as conn:
        return {"claims": queries.quarantine(conn, doc, limit, offset)}


@app.get("/metrics")
def list_metrics():
    with connection() as conn:
        return {"metrics": queries.metrics(conn)}


@app.get("/entities")
def list_entities():
    with connection() as conn:
        return {"entities": queries.entities(conn)}


@app.get("/answer")
def get_answer(entity: str, metric: str, period: str | None = None):
    with connection() as conn:
        return queries.answer(conn, entity, metric, period)


@app.get("/pages/{doc_id}/{page_no}.png")
def page_png(doc_id: int, page_no: int, claim: int | None = None, dpi: int = Query(110, ge=50, le=200)):
    with connection() as conn:
        doc = db.one(conn, "select pdf, page_count from documents where id = ?", (doc_id,))
        if not doc:
            raise HTTPException(404, "No such document.")
        if page_no < 1 or page_no > doc["page_count"]:
            raise HTTPException(404, "No such page.")
        boxes = []
        if claim:
            found = db.one(conn, "select bbox_json, page_no from evidence where claim_id = ?", (claim,))
            if found and found["page_no"] == page_no:
                boxes = json.loads(found["bbox_json"] or "[]")
        pdf_bytes = doc["pdf"]
    pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = pdf[page_no - 1]
    for box in boxes:
        page.draw_rect(pymupdf.Rect(*box), color=(0.85, 0.2, 0.1), fill=(1, 0.85, 0.3), fill_opacity=0.35, width=1.2)
    pix = page.get_pixmap(dpi=dpi)
    return Response(pix.tobytes("png"), media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
