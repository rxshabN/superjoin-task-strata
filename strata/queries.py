import json

from . import db
from .canon import entity_key, parse_period, published_key
from .reconcile import RANK

CLAIM_SELECT = """
select c.id, c.doc_id, c.page_no, c.subject, c.label, c.metric_raw, c.value_raw, c.unit_raw, c.period_raw,
       c.scope_raw, c.basis_raw, c.quote, c.keys_json, c.status,
       d.filename, d.title, d.publisher, d.published_at, d.model,
       e.grade, e.page_no as located_page, e.bbox_json,
       cc.metric_key, cc.value_canon, cc.value_text, cc.unit_canon, cc.period_start, cc.period_end,
       cc.scope_canon, cc.basis_canon, cc.block_key, cc.precision, en.name_canon as entity, en.id as entity_id,
       q.reason as quarantine_reason
from claims c
join documents d on d.id = c.doc_id
left join evidence e on e.claim_id = c.id
left join claim_canon cc on cc.claim_id = c.id
left join entities en on en.id = cc.entity_id
left join quarantine q on q.claim_id = c.id
"""


def _shape(row: dict) -> dict:
    row["keys"] = json.loads(row.pop("keys_json") or "{}")
    row["bboxes"] = json.loads(row.pop("bbox_json") or "[]")
    return row


def documents(conn) -> list[dict]:
    rows = db.rows(
        conn,
        """
        select d.id, d.filename, d.title, d.publisher, d.published_at, d.model, d.page_count, d.status, d.error,
               d.malformed_lines, d.ingested_at,
               (select count(*) from claims c where c.doc_id = d.id) as claims,
               (select count(*) from claims c where c.doc_id = d.id and c.status = 'verified') as verified,
               (select count(*) from claims c where c.doc_id = d.id and c.status = 'quarantined') as quarantined,
               (select count(*) from claims c join evidence e on e.claim_id = c.id
                 where c.doc_id = d.id and e.grade = 'exact') as exact,
               (select count(*) from claims c join evidence e on e.claim_id = c.id
                 where c.doc_id = d.id and e.grade = 'tokens') as weak,
               (select count(*) from pages p where p.doc_id = d.id and p.skipped = 1) as skipped_pages
        from documents d order by d.id
        """,
    )
    return rows


def document(conn, doc_id: int) -> dict | None:
    return next((d for d in documents(conn) if d["id"] == doc_id), None)


def claims(conn, doc_id=None, entity=None, metric=None, grade=None, status=None, q=None, limit=200, offset=0):
    where, params = [], []
    if doc_id:
        where.append("c.doc_id = ?")
        params.append(doc_id)
    if entity:
        where.append("en.name_canon like ?")
        params.append(f"%{entity_key(entity)}%")
    if metric:
        where.append("cc.metric_key = ?")
        params.append(metric)
    if grade:
        where.append("e.grade = ?")
        params.append(grade)
    if status:
        where.append("c.status = ?")
        params.append(status)
    if q:
        where.append("(c.label like ? or c.quote like ? or c.subject like ?)")
        params += [f"%{q}%"] * 3
    sql = CLAIM_SELECT + (" where " + " and ".join(where) if where else "") + " order by c.doc_id, c.page_no, c.id"
    sql += " limit ? offset ?"
    params += [limit, offset]
    return [_shape(r) for r in db.rows(conn, sql, tuple(params))]


def claim(conn, claim_id: int) -> dict | None:
    rows = db.rows(conn, CLAIM_SELECT + " where c.id = ?", (claim_id,))
    return _shape(rows[0]) if rows else None


def claims_by_ids(conn, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: _shape(r) for r in db.rows(conn, CLAIM_SELECT + f" where c.id in ({marks})", tuple(ids))}


def relations(conn, kind=None, cross_only=True, confidence=None, doc_id=None, metric=None, limit=200, offset=0):
    where, params = [], []
    if cross_only:
        where.append("a.doc_id != b.doc_id")
    if kind:
        where.append("r.kind = ?")
        params.append(kind)
    if confidence:
        where.append("r.confidence = ?")
        params.append(confidence)
    if doc_id:
        where.append("(a.doc_id = ? or b.doc_id = ?)")
        params += [doc_id, doc_id]
    if metric:
        where.append("cc.metric_key = ?")
        params.append(metric)
    sql = (
        "select r.id, r.a_id, r.b_id, r.kind, r.dimension, r.explanation, r.confidence, cc.metric_key"
        " from relations r join claims a on a.id = r.a_id join claims b on b.id = r.b_id"
        " join claim_canon cc on cc.claim_id = r.a_id"
    )
    sql += (" where " + " and ".join(where) if where else "") + " order by r.kind, cc.metric_key, r.id limit ? offset ?"
    params += [limit, offset]
    rows = db.rows(conn, sql, tuple(params))
    lookup = claims_by_ids(conn, sorted({i for r in rows for i in (r["a_id"], r["b_id"])}))
    for r in rows:
        r["a"] = lookup.get(r["a_id"])
        r["b"] = lookup.get(r["b_id"])
    return rows


def relation(conn, relation_id: int) -> dict | None:
    rows = db.rows(
        conn,
        "select id, a_id, b_id, kind, dimension, explanation, confidence from relations where id = ?",
        (relation_id,),
    )
    if not rows:
        return None
    r = rows[0]
    lookup = claims_by_ids(conn, [r["a_id"], r["b_id"]])
    r["a"], r["b"] = lookup.get(r["a_id"]), lookup.get(r["b_id"])
    return r


def relation_counts(conn) -> dict:
    rows = db.rows(
        conn,
        "select r.kind, a.doc_id != b.doc_id as cross_doc, count(*) as n from relations r"
        " join claims a on a.id = r.a_id join claims b on b.id = r.b_id group by r.kind, cross_doc",
    )
    out = {"cross": {}, "within": {}}
    for r in rows:
        out["cross" if r["cross_doc"] else "within"][r["kind"]] = r["n"]
    return out


def quarantine(conn, doc_id=None, limit=200, offset=0):
    where = " and c.doc_id = ?" if doc_id else ""
    params = (doc_id, limit, offset) if doc_id else (limit, offset)
    return [
        _shape(r)
        for r in db.rows(
            conn,
            CLAIM_SELECT + " where c.status = 'quarantined'" + where + " order by c.doc_id, c.page_no limit ? offset ?",
            params,
        )
    ]


def metrics(conn) -> list[dict]:
    rows = db.rows(
        conn,
        "select m.key, m.label, m.aliases_json, m.claim_count, m.first_seen_doc, d.filename as first_seen"
        " from metrics m left join documents d on d.id = m.first_seen_doc order by m.claim_count desc, m.key",
    )
    for r in rows:
        r["aliases"] = json.loads(r.pop("aliases_json") or "[]")
    return rows


def entities(conn) -> list[dict]:
    rows = db.rows(
        conn,
        "select en.id, en.name_canon, en.kind, en.keys_json, count(cc.claim_id) as claims"
        " from entities en left join claim_canon cc on cc.entity_id = en.id"
        " group by en.id order by claims desc, en.name_canon",
    )
    for r in rows:
        r["keys"] = json.loads(r.pop("keys_json") or "{}")
    return rows


def _rank(c: dict) -> tuple:
    return (RANK.get(c.get("basis_canon"), 0), published_key(c.get("published_at")) or "", c.get("id") or 0)


def answer(conn, entity: str, metric: str, period: str | None = None) -> dict:
    canon_name = entity_key(entity)
    ent = db.one(conn, "select id, name_canon from entities where name_canon = ?", (canon_name,)) or db.one(
        conn,
        "select id, name_canon from entities where name_canon like ? order by length(name_canon) limit 1",
        (f"%{canon_name}%",),
    )
    if not ent:
        return {"found": False, "reason": f"no entity matching {entity!r}"}
    key = metric.strip().lower()
    known = db.one(conn, "select key from metrics where key = ?", (key,))
    if not known:
        alias = db.one(conn, "select key from metrics where aliases_json like ?", (f'%"{key}"%',))
        if alias:
            key = alias["key"]
    span = parse_period(period) if period else None
    if period and span is None:
        return {"found": False, "reason": f"could not read period {period!r}"}
    block = f"{ent['id']}|{key}|{span[0]}|{span[1]}" if span else f"{ent['id']}|{key}"
    members = [_shape(r) for r in db.rows(conn, CLAIM_SELECT + " where cc.block_key = ? order by c.id", (block,))]
    if not members:
        return {"found": False, "reason": "no grounded claim for that entity, metric and period", "block": block}
    ids = [m["id"] for m in members]
    marks = ",".join("?" * len(ids))
    rels = db.rows(
        conn,
        "select id, a_id, b_id, kind, dimension, explanation, confidence from relations"
        f" where a_id in ({marks}) and b_id in ({marks})",
        tuple(ids) * 2,
    )
    superseded = {r["b_id"] for r in rels if r["kind"] == "supersedes"}
    live = [m for m in members if m["id"] not in superseded] or members
    current = max(live, key=_rank)
    by_id = {m["id"]: m for m in members}

    def others(kind):
        out = []
        for r in rels:
            if r["kind"] != kind or current["id"] not in (r["a_id"], r["b_id"]):
                continue
            other = by_id[r["b_id"] if r["a_id"] == current["id"] else r["a_id"]]
            out.append(
                {
                    "claim": other,
                    "dimension": r["dimension"],
                    "explanation": r["explanation"],
                    "confidence": r["confidence"],
                }
            )
        return out

    history = []
    for r in rels:
        if r["kind"] == "supersedes":
            history.append({"claim": by_id[r["b_id"]], "superseded_by": r["a_id"], "explanation": r["explanation"]})
    return {
        "found": True,
        "block": block,
        "entity": ent["name_canon"],
        "metric": key,
        "period": list(span) if span else None,
        "current": current,
        "history": history,
        "corroborated_by": others("corroborates"),
        "contradicted_by": others("contradicts"),
        "reconciled_with": others("reconciled"),
        "members": len(members),
    }
