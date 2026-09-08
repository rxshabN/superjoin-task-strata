import logging
import time

from google import genai
from google.genai import errors, types

from .. import config
from .base import Response

log = logging.getLogger("strata.gemini")
RETRY_WAITS = {429: (20, 40), 503: ()}
TIMEOUT_MS = 600_000


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
        self.api_key = api_key or cfg.gemini_api_key
        self.project = cfg.gcp_project
        self.location = cfg.gcp_location
        self.model = model or cfg.gemini_model
        self.spacing_s = cfg.request_spacing_s if spacing_s is None else spacing_s
        self._client = None
        self._last = 0.0

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            options = types.HttpOptions(timeout=TIMEOUT_MS)
            if self.backend == "vertex":
                if not self.project:
                    raise RuntimeError("GEMINI_BACKEND=vertex needs GOOGLE_CLOUD_PROJECT")
                self._client = genai.Client(
                    vertexai=True, project=self.project, location=self.location, http_options=options
                )
            else:
                if not self.api_key:
                    raise RuntimeError("GEMINI_API_KEY is not set")
                self._client = genai.Client(api_key=self.api_key, http_options=options)
        return self._client

    def _config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(temperature=0, seed=7, max_output_tokens=65536)

    def _wait(self):
        gap = self.spacing_s - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)

    def generate(self, pdf_bytes: bytes, prompt: str) -> Response:
        return self._request(
            [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), types.Part.from_text(text=prompt)]
        )

    def generate_text(self, prompt: str) -> Response:
        return self._request([types.Part.from_text(text=prompt)])

    def _request(self, parts: list) -> Response:
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
