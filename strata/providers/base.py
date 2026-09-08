from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass
class Response:
    text: str
    finish_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    cached: bool = False

    def to_record(self) -> dict:
        record = asdict(self)
        record.pop("cached")
        return record

    @classmethod
    def from_record(cls, record: dict) -> "Response":
        return cls(
            text=record["text"],
            finish_reason=record["finish_reason"],
            input_tokens=record.get("input_tokens"),
            output_tokens=record.get("output_tokens"),
            model=record.get("model", ""),
            cached=True,
        )


class Provider(Protocol):
    name: str
    model: str

    def generate(self, pdf_bytes: bytes, prompt: str) -> Response: ...
