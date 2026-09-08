from .. import config
from ..cache import Cache
from .base import Provider, Response


def get_provider(name: str | None = None, **kwargs) -> Provider:
    name = name or config.settings.provider
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(**kwargs)
    if name == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(**kwargs)
    raise ValueError(f"unknown provider {name!r}")


def generate(
    pdf_bytes: bytes,
    prompt: str,
    provider: Provider,
    cache: Cache | None = None,
    key: str | None = None,
) -> Response:
    cache = cache or Cache()
    key = key or cache.key(provider.name, provider.model, prompt, pdf_bytes)
    hit = cache.get(key)
    if hit:
        return Response.from_record(hit)
    response = provider.generate(pdf_bytes, prompt)
    cache.put(key, response.to_record())
    return response


__all__ = ["Cache", "Provider", "Response", "generate", "get_provider"]
