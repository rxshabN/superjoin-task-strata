from strata import ask
from strata.providers.base import Response


class Scripted:
    name = "scripted"
    model = "s-1"

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def generate_text(self, prompt):
        self.calls += 1
        return Response(text=self.text, finish_reason="STOP", output_tokens=5, model=self.model)


def test_parse_requires_listed_coordinates():
    entities, metrics = {"india"}, {"gdp_growth"}
    assert ask.parse('{"entity":"India","metric":"gdp_growth","period":"FY25"}', entities, metrics) == {
        "entity": "india",
        "metric": "gdp_growth",
        "period": "FY25",
    }
    fenced = '```json\n{"entity":"india","metric":"gdp_growth","period":null}\n```'
    assert ask.parse(fenced, entities, metrics) == {"entity": "india", "metric": "gdp_growth", "period": None}
    assert ask.parse('{"entity":"france","metric":"gdp_growth"}', entities, metrics) is None
    assert ask.parse('{"entity":"india","metric":"population"}', entities, metrics) is None
    assert ask.parse("no json here", entities, metrics) is None


def test_build_prompt_lists_entities_and_metrics():
    prompt = ask.build_prompt("What was India's GDP growth in FY25?", [("india", 3)], [("gdp_growth", 2)])
    assert "Entities (claims): india (3)" in prompt and "Metrics (claims): gdp_growth (2)" in prompt
    assert prompt.endswith("Question: What was India's GDP growth in FY25?")


def test_ask_endpoint_answers_and_replays(client, monkeypatch):
    from strata import api

    scripted = Scripted('{"entity":"india","metric":"gdp_growth","period":"FY25"}')
    monkeypatch.setattr(api.providers, "get_provider", lambda *a, **k: scripted)
    question = {"question": "What was India's GDP growth in FY25?"}
    data = client.post("/ask", json=question).json()
    assert data["found"] and data["coordinates"] == {"entity": "india", "metric": "gdp_growth", "period": "FY25"}
    assert data["current"]["value_raw"] == "6.5" and data["cached"] is False
    again = client.post("/ask", json=question).json()
    assert again["cached"] is True and scripted.calls == 1
    monkeypatch.setattr(api.providers, "get_provider", lambda *a, **k: Scripted('{"entity":null,"metric":null}'))
    miss = client.post("/ask", json={"question": "How tall is the Eiffel tower?"}).json()
    assert miss["found"] is False and miss["coordinates"] is None
    assert client.post("/ask", json={"question": "   "}).status_code == 400
