import dataclasses

import pytest

from strata import config, db
from strata.cache import Cache
from strata.providers import generate, get_provider
from strata.providers.base import Response

TABLES = {
    "documents",
    "pages",
    "claims",
    "evidence",
    "quarantine",
    "entities",
    "metrics",
    "claim_canon",
    "relations",
}


def test_settings_defaults():
    s = config.load({})
    assert s.provider == "gemini"
    assert s.gemini_model == "gemini-3.8-flash"
    assert s.pages_per_request == 50
    assert s.claims_per_page == 8
    assert s.db == "sqlite"
    assert s.cache_enabled is True


def test_settings_from_env():
    s = config.load({"STRATA_PROVIDER": "ollama", "OLLAMA_MODEL": "x", "STRATA_DB": "turso"})
    assert s.provider == "ollama"
    assert s.model == "x"
    assert db.backend(s) == "turso"
    v = config.load({"GEMINI_BACKEND": "vertex", "GOOGLE_CLOUD_PROJECT": "p1"})
    assert v.gemini_backend == "vertex" and v.gcp_project == "p1" and v.gcp_location == "global"
    assert config.load({}).gemini_backend == "aistudio"


def test_vertex_backend_requires_project(monkeypatch):
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, gemini_backend="vertex", gcp_project=None)
    )
    provider = get_provider("gemini")
    assert provider.model == config.settings.gemini_model
    with pytest.raises(RuntimeError):
        _ = provider.client


def test_gemini_provider_needs_a_key_only_for_a_live_request(monkeypatch):
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, gemini_backend="aistudio", gemini_api_key=None)
    )
    provider = get_provider("gemini")
    assert provider.name == "gemini"
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        provider.generate_text("hello")


def test_schema_applies(tmp_path):
    conn = db.init(db.connect(path=tmp_path / "t.db"))
    assert TABLES <= set(db.tables(conn))
    db.init(conn)
    conn.execute("insert into documents (sha256, filename) values (?, ?)", ("abc", "a.pdf"))
    db.commit(conn)
    assert db.one(conn, "select filename from documents where sha256 = ?", ("abc",)) == {"filename": "a.pdf"}


def test_turso_requires_credentials(tmp_path):
    s = dataclasses.replace(config.load({}), db="turso")
    with pytest.raises(RuntimeError):
        db.connect(s, path=tmp_path / "r.db")


def test_cache_roundtrip(tmp_path):
    c = Cache(tmp_path, enabled=True)
    k = c.key("gemini", "m", "prompt", b"pdf")
    assert k == c.key("gemini", "m", "prompt", b"pdf")
    assert k != c.key("gemini", "m", "promp", b"tpdf")
    assert c.get(k) is None
    c.put(k, {"text": "x", "finish_reason": "STOP"})
    assert c.get(k)["text"] == "x"
    assert Cache(tmp_path, enabled=False).get(k) is None


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.calls = 0

    def generate(self, pdf_bytes, prompt):
        self.calls += 1
        return Response(text="ok", finish_reason="STOP", output_tokens=1, model=self.model)


def test_generate_uses_cache(tmp_path):
    p = FakeProvider()
    c = Cache(tmp_path, enabled=True)
    first = generate(b"pdf", "q", p, c)
    second = generate(b"pdf", "q", p, c)
    assert p.calls == 1
    assert first.cached is False and second.cached is True
    assert second.text == "ok" and second.finish_reason == "STOP" and second.output_tokens == 1
    generate(b"pdf", "q", p, c, key="explicit")
    assert p.calls == 2 and c.get("explicit")["text"] == "ok"


def test_provider_selection():
    assert get_provider("gemini", api_key="test-key").name == "gemini"
    assert get_provider("ollama").name == "ollama"
    with pytest.raises(ValueError):
        get_provider("nope")


def test_health(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from strata import api

    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, db="sqlite", db_path=tmp_path / "h.db")
    )
    with TestClient(api.app) as client:
        health = client.get("/health").json()
        assert health["ok"] is True and health["db"] == "sqlite" and health["tables"] >= 9
        assert "Strata" in client.get("/").text
