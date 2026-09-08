import json

from . import db, providers
from .cache import Cache

CLASSES = {"pct": "rate", "count": "count", "x": "ratio"}
SAMPLE_LABELS = 3


def unit_class(unit: str | None) -> str | None:
    if unit is None:
        return None
    return CLASSES.get(unit, "amount")


def dominant_units(conn) -> dict[str, str]:
    rows = db.rows(
        conn,
        "select metric_key, unit_canon, count(*) as n from claim_canon where unit_canon is not null"
        " group by metric_key, unit_canon order by metric_key, n desc",
    )
    units: dict[str, str] = {}
    for r in rows:
        units.setdefault(r["metric_key"], r["unit_canon"])
    return units


def registry_rows(conn) -> list[dict]:
    units = dominant_units(conn)
    labels: dict[str, list[str]] = {}
    for r in db.rows(
        conn,
        "select distinct cc.metric_key as key, c.label from claim_canon cc join claims c on c.id = cc.claim_id"
        " where c.label is not null order by cc.metric_key, c.id",
    ):
        seen = labels.setdefault(r["key"], [])
        if len(seen) < SAMPLE_LABELS and r["label"] not in seen:
            seen.append(r["label"])
    return [
        {"key": r["key"], "claims": r["claim_count"], "unit": units.get(r["key"]), "labels": labels.get(r["key"], [])}
        for r in db.rows(conn, "select key, claim_count from metrics order by key")
    ]


def build_prompt(rows: list[dict]) -> str:
    out = [
        "You are consolidating the metric registry of a fact layer. Each line below is one metric key with the",
        "unit its figures carry, how many claims use it, and up to three labels the documents used for it.",
        "",
        "Two keys name the same measure only when an analyst would put their figures in one row of one table:",
        "the same quantity, for the same kind of entity, in the same class of unit.",
        "Never merge a level with a growth rate or a share, a total with one of its components, two different",
        "bases, or keys whose units differ.",
        "",
        "Return one JSON object per line and nothing else: no prose, no code fences, no blank lines.",
        '{"keep":"<key>","merge":["<key>","<key>"],"why":"<at most ten words>"}',
        "keep is the key with more claims, or the more general name. A key may appear in one group only.",
        "Return nothing at all if no keys should merge.",
        "",
        "key | unit | claims | labels",
    ]
    for r in rows:
        out.append(f"{r['key']} | {r['unit'] or '?'} | {r['claims']} | {' ; '.join(r['labels'])}")
    return "\n".join(out)


def parse(text: str) -> list[dict]:
    groups = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("keep"), str):
            continue
        merge = [m for m in obj.get("merge") or [] if isinstance(m, str)]
        if merge:
            groups.append({"keep": obj["keep"], "merge": merge, "why": str(obj.get("why") or "")})
    return groups


def apply(conn, groups: list[dict]) -> dict:
    known = {r["key"] for r in db.rows(conn, "select key from metrics")}
    aliased = {r["alias"] for r in db.rows(conn, "select alias from metric_aliases")}
    units = dominant_units(conn)
    merged = {alias for g in groups for alias in g["merge"]}
    applied, rejected, used = [], [], set()
    for g in groups:
        keep = g["keep"]
        if keep not in known or keep in aliased:
            rejected.append((keep, None, "keep is not a live key"))
            continue
        if keep in merged:
            rejected.append((keep, None, "keep is merged away in another group"))
            continue
        for alias in g["merge"]:
            reason = None
            if alias == keep or alias not in known:
                reason = "unknown key"
            elif alias in used or alias in aliased:
                reason = "already used"
            elif (
                unit_class(units.get(alias))
                and unit_class(units.get(keep))
                and unit_class(units[alias]) != unit_class(units[keep])
            ):
                reason = f"unit differs ({units[alias]} vs {units[keep]})"
            if reason:
                rejected.append((alias, keep, reason))
                continue
            used.add(alias)
            conn.execute(
                "insert or replace into metric_aliases (alias, key, source) values (?, ?, ?)",
                (alias, keep, g.get("why") or "consolidation"),
            )
            applied.append((alias, keep))
    db.commit(conn)
    return {"applied": applied, "rejected": rejected}


def consolidate(conn, provider=None, cache: Cache | None = None) -> dict:
    provider = provider or providers.get_provider()
    rows = registry_rows(conn)
    response = providers.generate(None, build_prompt(rows), provider, cache or Cache())
    groups = parse(response.text)
    stats = apply(conn, groups)
    stats.update(
        {"keys": len(rows), "proposed": groups, "cached": response.cached, "tokens_out": response.output_tokens}
    )
    return stats
