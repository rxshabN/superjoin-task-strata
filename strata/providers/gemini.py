import logging
import time

from google import genai
from google.genai import errors, types

from .. import config
from .base import Response

log = logging.getLogger("strata.gemini")
RETRY_WAITS = {429: (20, 40), 503: ()}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        spacing_s: float | None = None,
        backend: str | None = None,
    ):
        cfg = config.settings
        self.backend = backend or cfg.gemini_backend
        if self.backend == "vertex":
            if not cfg.gcp_project:
                raise RuntimeError("GEMINI_BACKEND=vertex needs GOOGLE_CLOUD_PROJECT")
            self.client = genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)
        else:
            key = api_key or cfg.gemini_api_key
            if not key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self.client = genai.Client(api_key=key)
        self.model = model or cfg.gemini_model
        self.spacing_s = cfg.request_spacing_s if spacing_s is None else spacing_s
        self._last = 0.0

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(temperature=0, seed=7, max_output_tokens=65536)

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
        retries = {429: 0, 503: 0}
        while result is None:
            self._wait()
            self._last = time.monotonic()
            try:
                result = self.client.models.generate_content(model=self.model, contents=parts, config=self._config())
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code in RETRY_WAITS and retries[code] < len(RETRY_WAITS[code]):
                    wait = RETRY_WAITS[code][retries[code]]
                    retries[code] += 1
                    log.warning("gemini %s, retry %d after %ds", code, retries[code], wait)
                    time.sleep(wait)
                    continue
                raise
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
