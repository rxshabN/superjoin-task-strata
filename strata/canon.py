import calendar
import json
import re
from datetime import date, timedelta

from . import db

FY_END_MONTH = 3

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9

CURRENCIES = {
    "inr": "INR",
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "rupees": "INR",
    "rupee": "INR",
    "indian rupees": "INR",
    "usd": "USD",
    "us$": "USD",
    "$": "USD",
    "us dollars": "USD",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
}

SCALES = {
    "thousand": 1e3,
    "thousands": 1e3,
    "k": 1e3,
    "'000": 1e3,
    "000s": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "million": 1e6,
    "millions": 1e6,
    "mn": 1e6,
    "mm": 1e6,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "billion": 1e9,
    "billions": 1e9,
    "bn": 1e9,
    "trillion": 1e12,
    "tn": 1e12,
}

PCT = {
    "percent",
    "per cent",
    "%",
    "pct",
    "percentage",
    "percentage points",
    "pp",
    "ppt",
    "percent of gdp",
    "per cent of gdp",
}
BPS = {"bps", "basis points", "basis point"}
COUNT = {"count", "number", "nos", "no", "units", "unit", "n"}

BASIS_MARKERS = {
    "p": "provisional",
    "pe": "provisional",
    "prov": "provisional",
    "re": "revised",
    "rev": "revised",
    "be": "budget",
    "ae": "advance_estimate",
    "fae": "advance_estimate",
    "sae": "advance_estimate",
    "e": "projection",
    "f": "projection",
    "proj": "projection",
}

HONORIFICS = {"mr", "mrs", "ms", "dr", "shri", "smt", "prof", "sh", "sri", "mx"}
SUFFIXES = {"limited", "ltd", "inc", "llc", "plc", "pvt", "private", "corporation", "corp", "co", "company", "group"}


def squash(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _year(token: str, reference: int | None = None) -> int:
    n = int(token)
    if n >= 100:
        return n
    if reference and len(token) == 2:
        return reference // 100 * 100 + n
    return 2000 + n


def _fy(end_year: int) -> tuple[date, date]:
    end = date(end_year, FY_END_MONTH, calendar.monthrange(end_year, FY_END_MONTH)[1])
    start = date(end_year - 1, FY_END_MONTH, end.day) + timedelta(days=1)
    return start, end


def _fy_end_year(first: str, second: str | None) -> int:
    if second:
        return _year(second, _year(first))
    return _year(first)


def _parse_date(text: str) -> date | None:
    s = squash(text).replace(",", "")
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)? (\d{4})", s)
    if m and m[1] in MONTHS:
        return date(int(m[3]), MONTHS[m[1]], int(m[2]))
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)\.? (\d{4})", s)
    if m and m[2] in MONTHS:
        return date(int(m[3]), MONTHS[m[2]], int(m[1]))
    return None


def _month_span(month: int, year: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _months_before(end: date, months: int) -> date:
    month = end.month - months + 1
    year = end.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def basis_marker(raw) -> str | None:
    for m in re.finditer(r"\(([a-z]{1,4})\)", squash(raw)):
        if m[1] in BASIS_MARKERS:
            return BASIS_MARKERS[m[1]]
    return None


BASIS_PHRASES = (
    ("advance estimate", "advance_estimate"),
    ("provisional", "provisional"),
    ("revised estimate", "revised"),
    ("budget estimate", "budget"),
    ("projected", "projection"),
    ("projection", "projection"),
    ("forecast", "projection"),
    ("pro forma", "pro_forma"),
    ("pro-forma", "pro_forma"),
)


def basis_phrase(*texts) -> str | None:
    blob = squash(" ".join(str(t) for t in texts if t))
    for phrase, basis in BASIS_PHRASES:
        if phrase in blob:
            return basis
    return None


NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

MONTH_RE = (
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
    + "|".join(m for m in MONTHS if len(m) <= 4)
    + r")"
)


def parse_period(raw) -> tuple[str, str] | None:
    s = squash(raw)
    s = s.replace("–", "-").replace("—", "-").replace("’", "'").replace("‘", "'")
    s = re.sub(r"[*†#]+$", "", s).strip()
    if not s or s in ("null", "none"):
        return None
    span = _parse_span(s)
    if span is None:
        s = re.sub(r"\([^)]*\)", "", s).strip()
        s = re.sub(r"^(q[1-4]|h[12]) of ", r"\1 ", s)
        span = _parse_span(s) if s else None
    if span is None:
        return None
    start, end = span
    return start.isoformat(), end.isoformat()


def _month_range(m1: str, m2: str, year: int, end_year: int | None = None) -> tuple[date, date]:
    a, b = MONTHS[m1], MONTHS[m2]
    end_year = end_year or year
    start_year = year if end_year == year and a <= b else (end_year - 1 if end_year == year else year)
    return date(start_year, a, 1), date(end_year, b, calendar.monthrange(end_year, b)[1])


def _fy_months(end_year: int, m1: int, m2: int) -> tuple[date, date]:
    start_year = end_year - 1 if m1 >= FY_END_MONTH + 1 else end_year
    stop_year = end_year - 1 if m2 >= FY_END_MONTH + 1 else end_year
    return date(start_year, m1, 1), date(stop_year, m2, calendar.monthrange(stop_year, m2)[1])


def _point(span: tuple[date, date] | None) -> tuple[date, date] | None:
    return (span[1], span[1]) if span else None


def _parse_span(s: str) -> tuple[date, date] | None:
    fy_tail = r"(?:fy|fiscal(?: year)?|financial year)?\s*'?(\d{2}|\d{4})(?:\s*[-/]\s*'?(\d{2}|\d{4}))?"
    m = re.fullmatch(r"(?:as at |as on )?end[- ](?:of[- ])?(.+)", s)
    if m:
        return _point(_parse_span(m[1]))
    m = re.fullmatch(
        r"(?:fy|fiscal(?: year)?|financial year)\s*'?(\d{2}|\d{4})\s*\(("
        + MONTH_RE
        + r")\s*(?:-|to)\s*("
        + MONTH_RE
        + r")\)",
        s,
    )
    if m:
        return _fy_months(_year(m[1]), MONTHS[m[2]], MONTHS[m[3]])
    m = re.fullmatch(r"first (\w+) months of " + fy_tail, s)
    if m and m[1] in NUMBER_WORDS:
        start = _fy(_fy_end_year(m[2], m[3]))[0]
        month = start.month + NUMBER_WORDS[m[1]] - 1
        stop_year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return start, date(stop_year, month, calendar.monthrange(stop_year, month)[1])
    m = re.fullmatch(r"(?:for the )?(\w+) months(?: period)? ended (?:on )?(.+)", s)
    if m and m[1] in NUMBER_WORDS:
        end = _parse_date(m[2])
        if end:
            return _months_before(end, NUMBER_WORDS[m[1]]), end
    m = re.fullmatch(r"(" + MONTH_RE + r")\s+(\d{4})\s*(?:-|to)\s*(" + MONTH_RE + r")\s+(\d{4})", s)
    if m:
        return _month_range(m[1], m[3], int(m[2]), int(m[4]))
    m = re.fullmatch(r"(" + MONTH_RE + r")\s*(?:-|to)\s*(" + MONTH_RE + r")\s+(\d{4})", s)
    if m:
        return _month_range(m[1], m[2], int(m[3]))
    m = re.fullmatch(r"q([1-4])\s+(?:of\s+)?(\d{4})", s)
    if m:
        y, q = int(m[2]), int(m[1])
        return date(y, 3 * q - 2, 1), date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
    m = re.fullmatch(r"q([1-4])\s*" + fy_tail, s)
    if m:
        y = _fy_end_year(m[2], m[3])
        q = int(m[1])
        start = date(y - 1, 3 * q + 1, 1) if q < 4 else date(y, 1, 1)
        end_month = 3 * q + 3 if q < 4 else 3
        end_year = y - 1 if q < 4 else y
        return start, date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    m = re.fullmatch(r"h([12])\s*" + fy_tail, s)
    if m:
        y = _fy_end_year(m[2], m[3])
        return (date(y - 1, 4, 1), date(y - 1, 9, 30)) if m[1] == "1" else (date(y - 1, 10, 1), date(y, 3, 31))
    m = re.fullmatch(r"(?:fy|fiscal(?: year)?|financial year)\s*'?(\d{2}|\d{4})(?:\s*[-/]\s*'?(\d{2}|\d{4}))?", s)
    if m:
        return _fy(_fy_end_year(m[1], m[2]))
    m = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})", s)
    if m:
        return _fy(_fy_end_year(m[1], m[2]))
    m = re.fullmatch(r"(?:for the )?(?:(?:financial |fiscal )?year|twelve months|12 months) ended (?:on )?(.+)", s)
    if m:
        end = _parse_date(m[1])
        if end:
            return _months_before(end, 12), end
    m = re.fullmatch(r"(?:for the )?(?:quarter|three months) ended (?:on )?(.+)", s)
    if m:
        end = _parse_date(m[1])
        if end:
            return _months_before(end, 3), end
    m = re.fullmatch(r"(?:as at|as on|as of|at|on)\s+(.+)", s)
    if m:
        d = _parse_date(m[1])
        if d:
            return d, d
    d = _parse_date(s)
    if d:
        return d, d
    m = re.fullmatch(r"(\d{4})\s*q([1-4])", s)
    if m:
        y, q = int(m[1]), int(m[2])
        return date(y, 3 * q - 2, 1), date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
    m = re.fullmatch(r"(?:cy\s*)?(\d{4})", s)
    if m:
        y = int(m[1])
        return date(y, 1, 1), date(y, 12, 31)
    m = re.fullmatch(r"([a-z]+)\.?\s*'?(\d{2}|\d{4})", s)
    if m and m[1] in MONTHS:
        return _month_span(MONTHS[m[1]], _year(m[2]))
    m = re.fullmatch(r"(?:end[- ])?(?:march|mar)\s*'?(\d{2}|\d{4})", s)
    if m:
        return _month_span(3, _year(m[1]))
    return None


def parse_unit(raw) -> tuple[str, float] | None:
    s = squash(raw)
    if not s or s in ("null", "none"):
        return None
    if s in BPS:
        return "pct", 0.01
    if s in PCT or "%" in s or "percent" in s or "per cent" in s:
        return "pct", 1.0
    if s in COUNT:
        return "count", 1.0
    if s in ("x", "times", "multiple"):
        return "x", 1.0
    tokens = re.findall(r"us\$|[₹€$£]|[a-z']+|\d+s?|'000", s)
    currency = None
    scale = 1.0
    rest = []
    for token in tokens:
        t = token.rstrip(".")
        if t in CURRENCIES:
            currency = CURRENCIES[t]
        elif t in SCALES:
            scale = SCALES[t]
        elif t in ("in", "of", "amount", "amounts"):
            continue
        else:
            rest.append(t)
    if currency:
        return currency, scale
    if rest:
        return " ".join(rest), scale
    if scale != 1.0:
        return "count", scale
    return s, 1.0


def parse_value(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("₹", "").replace("$", "").replace("%", "").replace(",", "").strip()
    s = re.sub(r"^(rs\.?|inr|usd)\s*", "", s, flags=re.I)
    if s.startswith("-"):
        negative = True
        s = s[1:]
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def precision_of(raw) -> float:
    s = str(raw or "").strip().strip("()").replace(",", "")
    m = re.search(r"\d+(?:\.(\d+))?", s)
    if not m:
        return 0.0
    decimals = len(m[1]) if m[1] else 0
    return 0.5 * 10 ** (-decimals)


def entity_key(name) -> str:
    words = re.sub(r"[^\w\s]", " ", squash(name)).split()
    while words and words[0] in HONORIFICS:
        words.pop(0)
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


def published_key(raw) -> str | None:
    s = squash(raw)
    m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", s)
    if m:
        return f"{m[1]}-{m[2] or '01'}-{m[3] or '01'}"
    d = _parse_date(s)
    if d:
        return d.isoformat()
    m = re.fullmatch(r"([a-z]+)\.?,? (\d{4})", s)
    if m and m[1] in MONTHS:
        return f"{m[2]}-{MONTHS[m[1]]:02d}-01"
    return None


def resolve_entity(conn, name: str, keys: dict) -> int:
    canon = entity_key(name)
    for key, value in (keys or {}).items():
        if not value:
            continue
        for row in db.rows(conn, "select id, keys_json from entities"):
            if str(json.loads(row["keys_json"]).get(key, "")).lower() == str(value).lower():
                return row["id"]
    row = db.one(conn, "select id, keys_json from entities where name_canon = ?", (canon,))
    if row:
        merged = {**json.loads(row["keys_json"]), **{k: v for k, v in (keys or {}).items() if v}}
        if merged != json.loads(row["keys_json"]):
            conn.execute("update entities set keys_json = ? where id = ?", (json.dumps(merged), row["id"]))
        return row["id"]
    kind = "person" if any(k in (keys or {}) for k in ("din", "pan")) else None
    conn.execute(
        "insert into entities (name_canon, kind, keys_json) values (?, ?, ?)",
        (canon, kind, json.dumps({k: v for k, v in (keys or {}).items() if v})),
    )
    return db.one(conn, "select id from entities where name_canon = ?", (canon,))["id"]


def singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def same_measure(a: str, b: str) -> bool:
    return [singular(t) for t in a.split("_")] == [singular(t) for t in b.split("_")]


def resolve_metric(conn, key: str, label: str | None, doc_id: int) -> str:
    rows = db.rows(conn, "select key, aliases_json, claim_count from metrics")
    index = {r["key"]: json.loads(r["aliases_json"]) for r in rows}
    counts = {r["key"]: r["claim_count"] for r in rows}
    target = next((k for k, aliases in index.items() if key in aliases and k != key), None)
    if target is None:
        match = next((k for k in index if k != key and same_measure(key, k)), None)
        if match is None:
            if key not in index:
                conn.execute(
                    "insert into metrics (key, label, first_seen_doc, claim_count) values (?, ?, ?, 0)",
                    (key, label, doc_id),
                )
            return key
        target = match
        if key in index and counts.get(key, 0) > counts.get(target, 0):
            key, target = target, key
        index[target].append(key)
        conn.execute("update metrics set aliases_json = ? where key = ?", (json.dumps(index[target]), target))
    if key in index:
        conn.execute("update claim_canon set metric_key = ? where metric_key = ?", (target, key))
        conn.execute("update metrics set claim_count = claim_count + ? where key = ?", (counts[key], target))
        conn.execute("delete from metrics where key = ?", (key,))
    return target


def canonicalise_claim(conn, claim: dict) -> dict:
    keys = json.loads(claim.get("keys_json") or "{}")
    entity_id = resolve_entity(conn, claim["subject"] or "unknown", keys)
    metric = resolve_metric(conn, claim["metric_raw"], claim.get("label"), claim["doc_id"])
    period = parse_period(claim.get("period_raw"))
    unit = parse_unit(claim.get("unit_raw"))
    number = parse_value(claim.get("value_raw"))
    basis = claim.get("basis_raw") or "actual"
    if basis == "actual":
        basis = basis_marker(claim.get("period_raw")) or basis_phrase(claim.get("quote"), claim.get("label")) or basis
    scope = squash(claim.get("scope_raw"))
    scope = None if scope in ("", "null", "none") else scope
    row = {
        "claim_id": claim["id"],
        "entity_id": entity_id,
        "metric_key": metric,
        "period_start": period[0] if period else None,
        "period_end": period[1] if period else None,
        "unit_canon": unit[0] if unit else None,
        "value_canon": None,
        "value_text": None,
        "basis_canon": basis,
        "scope_canon": scope,
        "block_key": None,
        "precision": None,
    }
    if number is not None:
        if unit:
            row["value_canon"] = number * unit[1]
            row["precision"] = precision_of(claim.get("value_raw")) * unit[1]
        if unit and period:
            row["block_key"] = f"{entity_id}|{metric}|{period[0]}|{period[1]}"
    else:
        text = squash(claim.get("value_raw"))
        if text:
            row["value_text"] = text
            row["block_key"] = f"{entity_id}|{metric}"
    return row


def canonicalise_document(conn, doc_id: int) -> dict:
    claims = db.rows(
        conn,
        "select c.* from claims c left join claim_canon cc on cc.claim_id = c.id"
        " where c.doc_id = ? and c.status = 'verified' and cc.claim_id is null order by c.id",
        (doc_id,),
    )
    stats = {"numeric": 0, "text": 0, "non_comparable": 0}
    for claim in claims:
        row = canonicalise_claim(conn, claim)
        conn.execute(
            "insert into claim_canon (claim_id, entity_id, metric_key, period_start, period_end, unit_canon,"
            " value_canon, value_text, basis_canon, scope_canon, block_key, precision)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row.values()),
        )
        if row["block_key"] is None:
            stats["non_comparable"] += 1
        elif row["value_text"] is not None:
            stats["text"] += 1
        else:
            stats["numeric"] += 1
    db.commit(conn)
    return stats


def reset(conn):
    conn.execute("delete from relations")
    conn.execute("delete from claim_canon")
    conn.execute("delete from entities")
    conn.execute("delete from metrics")
    conn.execute(
        "insert into metrics (key, label, first_seen_doc, claim_count)"
        " select metric_raw, min(label), min(doc_id), count(*) from claims group by metric_raw"
    )
    db.commit(conn)
