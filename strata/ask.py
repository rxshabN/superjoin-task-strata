import json
import re

from . import providers
from .cache import Cache

MAX_LISTED = 300


def build_prompt(question: str, entities: list[tuple[str, int]], metrics: list[tuple[str, int]]) -> str:
    out = [
        "You turn a question into the coordinates of a fact lookup: one entity, one metric, one period.",
        "Choose the entity and the metric from the lists below, copied exactly. Write the period the way the",
        "question states it (FY25, 2024-25, Q4 FY24, March 31, 2024), or null when the question gives none.",
        "",
        "Return one JSON object and nothing else:",
        '{"entity":"<from the list>","metric":"<from the list>","period":"<as stated or null>"}',
        'If no listed entity or metric fits the question, return {"entity":null,"metric":null,"period":null}.',
        "",
        "Entities (claims): " + ", ".join(f"{name} ({n})" for name, n in entities[:MAX_LISTED]),
        "Metrics (claims): " + ", ".join(f"{key} ({n})" for key, n in metrics[:MAX_LISTED]),
        "",
        f"Question: {question.strip()}",
    ]
    return "\n".join(out)


def parse(text: str, entities: set[str], metrics: set[str]) -> dict | None:
    m = re.search(r"\{.*?\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    entity = str(obj.get("entity") or "").strip().lower()
    metric = str(obj.get("metric") or "").strip().lower()
    if entity not in entities or metric not in metrics:
        return None
    period = obj.get("period")
    return {"entity": entity, "metric": metric, "period": str(period).strip() if period else None}


def coordinates(
    question: str, entities: list[tuple[str, int]], metrics: list[tuple[str, int]], provider, cache: Cache | None = None
) -> tuple[dict | None, bool]:
    prompt = build_prompt(question, entities, metrics)
    response = providers.generate(None, prompt, provider, cache or Cache())
    found = parse(response.text, {e for e, _ in entities}, {k for k, _ in metrics})
    return found, response.cached
