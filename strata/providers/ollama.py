import base64
import json
import urllib.request

import pymupdf

from .. import config
from .base import Response


class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str | None = None, model: str | None = None, dpi: int = 110):
        cfg = config.settings
        self.url = (url or cfg.ollama_url).rstrip("/")
        self.model = model or cfg.ollama_model
        self.dpi = dpi

    def _images(self, pdf_bytes: bytes) -> list[str]:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        return [base64.b64encode(page.get_pixmap(dpi=self.dpi).tobytes("png")).decode("ascii") for page in doc]

    def generate(self, pdf_bytes: bytes, prompt: str) -> Response:
        body = {
            "model": self.model,
            "prompt": prompt,
            "images": self._images(pdf_bytes),
            "stream": False,
            "options": {"temperature": 0, "seed": 7, "num_predict": 16384},
        }
        request = urllib.request.Request(
            f"{self.url}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=1800) as response:
            data = json.load(response)
        done = data.get("done_reason", "stop")
        return Response(
            text=data.get("response", ""),
            finish_reason="STOP" if done == "stop" else str(done).upper(),
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            model=self.model,
        )
