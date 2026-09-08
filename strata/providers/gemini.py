import time

from google import genai
from google.genai import errors, types

from .. import config
from .base import Response


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None, spacing_s: float | None = None):
        cfg = config.settings
        key = api_key or cfg.gemini_api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self.client = genai.Client(api_key=key)
        self.model = model or cfg.gemini_model
        self.spacing_s = cfg.request_spacing_s if spacing_s is None else spacing_s
        self._last = 0.0
        self._thinking = True

    def _config(self) -> types.GenerateContentConfig:
        thinking = None
        if self._thinking:
            thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
        return types.GenerateContentConfig(
            temperature=0,
            seed=7,
            max_output_tokens=65536,
            thinking_config=thinking,
        )

    def _wait(self):
        gap = self.spacing_s - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)

    def generate(self, pdf_bytes: bytes, prompt: str) -> Response:
        parts = [
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            types.Part.from_text(text=prompt),
        ]
        result = None
        for attempt in range(5):
            self._wait()
            self._last = time.monotonic()
            try:
                result = self.client.models.generate_content(model=self.model, contents=parts, config=self._config())
                break
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code in (429, 503) and attempt < 4:
                    time.sleep(20 * (attempt + 1))
                    continue
                if code == 400 and self._thinking and "thinking" in str(e).lower():
                    self._thinking = False
                    continue
                raise
        if result is None:
            raise RuntimeError("gemini: no response after retries")
        candidate = result.candidates[0] if result.candidates else None
        finish = candidate.finish_reason.name if candidate and candidate.finish_reason else "UNKNOWN"
        usage = result.usage_metadata
        return Response(
            text=result.text or "",
            finish_reason=finish,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            model=self.model,
        )
