import re
from itertools import combinations

from . import db
from .canon import MONTHS, entity_key, published_key

RANK = {"advance_estimate": 1, "provisional": 2, "revised": 3, "actual": 4}
FALLBACK_RELATIVE = 0.005
LABEL_NOISE = {
    "the",
    "of",
    "from",
    "for",
    "in",
    "and",
    "to",
    "a",
    "an",
    "as",
    "at",
    "on",
    "by",
    "with",
    "total",
    "year",
    "ended",
}
LABEL_PERIOD = re.compile(r"^(fy\d*|q[1-4]|h[12]|cy\d*|\d{2}|\d{4})$")


def label_tokens(label) -> frozenset[str]:
    words = re.sub(r"[^\w\s]", " ", str(label or "").lower()).split()
    return frozenset(w for w in words if w not in LABEL_NOISE and w not in MONTHS and not LABEL_PERIOD.match(w))


def labels_differ(a, b) -> bool:
    ta, tb = label_tokens(a.get("label")), label_tokens(b.get("label"))
    return bool(ta) and bool(tb) and ta != tb


def close(a: float, b: float, precision: float | None = None) -> bool:
    if precision:
        return abs(a - b) <= precision * 1.0000001
    scale = max(abs(a), abs(b))
    return abs(a - b) <= FALLBACK_RELATIVE * scale if scale else True


def _relation(kind, a, b, dimension=None, explanation=None, confidence="high"):
    return {
        "kind": kind,
        "a_id": a["id"],
        "b_id": b["id"],
        "dimension": dimension,
        "explanation": explanation,
        "confidence": confidence,
    }


def _same_publisher(a, b) -> bool:
    return bool(a.get("publisher")) and entity_key(a["publisher"]) == entity_key(b.get("publisher"))


def _vintage(claim) -> str | None:
    return published_key(claim.get("published_at"))


def relate(a: dict, b: dict) -> dict | None:
    if a.get("value_text") is not None or b.get("value_text") is not None:
        return relate_text(a, b)
    if a.get("value_canon") is None or b.get("value_canon") is None:
        return None
    unit = a.get("unit_canon")
    if unit != b.get("unit_canon"):
        return None
    scopes_known = bool(a.get("scope_canon")) and bool(b.get("scope_canon"))
    one_unstated = bool(a.get("scope_canon")) != bool(b.get("scope_canon"))
    scope_differs = scopes_known and a["scope_canon"] != b["scope_canon"]
    basis_differs = a.get("basis_canon") != b.get("basis_canon")
    if close(a["value_canon"], b["value_canon"], max(a.get("precision") or 0, b.get("precision") or 0)):
        notes = []
        if scope_differs:
            notes.append(f"scope differs ({a['scope_canon']} vs {b['scope_canon']}) yet values agree")
        elif one_unstated:
            notes.append("scope unstated on one side")
        if basis_differs:
            notes.append(f"basis differs ({a['basis_canon']} vs {b['basis_canon']}) yet values agree")
        return _relation("corroborates", a, b, explanation="; ".join(notes) or None)
    if scope_differs:
        return _relation("reconciled", a, b, "scope", f"{a['scope_canon']} vs {b['scope_canon']}")
    if _same_publisher(a, b) and labels_differ(a, b):
        note = f"labels differ: {a.get('label')} vs {b.get('label')}"
        return _relation("reconciled", a, b, "label", f"same publisher, two line items; {note}", "low")
    if basis_differs:
        ra, rb = RANK.get(a["basis_canon"]), RANK.get(b["basis_canon"])
        if ra and rb:
            new, old = (a, b) if ra > rb else (b, a)
            return _relation(
                "supersedes", new, old, "basis", f"basis matured: {old['basis_canon']} → {new['basis_canon']}"
            )
        va, vb = _vintage(a), _vintage(b)
        if _same_publisher(a, b) and va and vb and va != vb:
            new, old = (a, b) if va > vb else (b, a)
            return _relation(
                "supersedes",
                new,
                old,
                "vintage",
                f"restated by {new['publisher']} on {_vintage(new)}: {old['basis_canon']} → {new['basis_canon']}",
            )
        return _relation("reconciled", a, b, "basis", f"{a['basis_canon']} vs {b['basis_canon']}")
    if one_unstated:
        return _relation("reconciled", a, b, "scope", "scope unstated on one side; values differ", confidence="low")
    if _same_publisher(a, b):
        va, vb = _vintage(a), _vintage(b)
        if va and vb and va != vb:
            new, old = (a, b) if va > vb else (b, a)
            return _relation(
                "supersedes", new, old, "vintage", f"restated by {new['publisher']} on {va if new is a else vb}"
            )
        if not (va and vb):
            return _relation(
                "contradicts", a, b, None, "publication date unknown, so supersession cannot be decided", "review"
            )
        return _relation("contradicts", a, b, None, "same publisher, same label, same date, different values", "review")
    return _relation("contradicts", a, b, None, "every coordinate matches and the values differ", "review")


def _as_of(claim) -> str | None:
    return claim.get("period_end") or _vintage(claim)


def relate_text(a: dict, b: dict) -> dict | None:
    ta, tb = a.get("value_text"), b.get("value_text")
    if ta is None or tb is None:
        return None
    if ta == tb:
        return _relation("corroborates", a, b)
    da, db_ = _as_of(a), _as_of(b)
    if da and db_ and da != db_:
        new, old = (a, b) if da > db_ else (b, a)
        return _relation(
            "supersedes",
            new,
            old,
            "time",
            f"state changed: {old['value_text']} ({_as_of(old)}) → {new['value_text']} ({_as_of(new)})",
        )
    return _relation("contradicts", a, b, None, "same entity, same as-of date, different states", "review")


CLAIM_SQL = """
select c.id, c.doc_id, c.label, cc.block_key, cc.unit_canon, cc.value_canon, cc.value_text, cc.basis_canon,
       cc.scope_canon, cc.period_start, cc.period_end, cc.precision, d.publisher, d.published_at
from claim_canon cc
join claims c on c.id = cc.claim_id
join documents d on d.id = c.doc_id
where cc.block_key is not null
"""


def reconcile(conn, block_keys: set[str] | None = None) -> dict:
    claims = db.rows(conn, CLAIM_SQL + " order by cc.block_key, c.id")
    blocks: dict[str, list[dict]] = {}
    for claim in claims:
        if block_keys is None or claim["block_key"] in block_keys:
            blocks.setdefault(claim["block_key"], []).append(claim)
    stats = {"blocks": 0, "pairs": 0, "corroborates": 0, "reconciled": 0, "supersedes": 0, "contradicts": 0}
    for members in blocks.values():
        if len(members) < 2:
            continue
        stats["blocks"] += 1
        for a, b in combinations(members, 2):
            rel = relate(a, b)
            if rel is None:
                continue
            stats["pairs"] += 1
            stats[rel["kind"]] += 1
            conn.execute(
                "delete from relations where (a_id = ? and b_id = ?) or (a_id = ? and b_id = ?)",
                (a["id"], b["id"], b["id"], a["id"]),
            )
            conn.execute(
                "insert into relations (a_id, b_id, kind, dimension, explanation, confidence)"
                " values (?, ?, ?, ?, ?, ?)",
                (rel["a_id"], rel["b_id"], rel["kind"], rel["dimension"], rel["explanation"], rel["confidence"]),
            )
    db.commit(conn)
    return stats


def blocks_for_document(conn, doc_id: int) -> set[str]:
    rows = db.rows(
        conn,
        "select distinct cc.block_key from claim_canon cc join claims c on c.id = cc.claim_id"
        " where c.doc_id = ? and cc.block_key is not null",
        (doc_id,),
    )
    return {r["block_key"] for r in rows}
