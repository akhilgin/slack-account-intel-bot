"""Message text -> decision (extract, then resolve the account)."""

from __future__ import annotations

from dataclasses import dataclass

from .extract import Extraction, extract
from .matching import Resolution, resolve_extraction


@dataclass
class Decision:
    status: str  # "match" | "ambiguous" | "none" | "not_intel"
    extraction: Extraction
    resolution: Resolution | None = None

    @property
    def method(self) -> str | None:
        return self.resolution.method if self.resolution else None


def analyze(text: str, cfg: dict, source, llm=None, summoned: bool = False) -> Decision:
    ex = extract(text, cfg, llm)
    if not ex.is_intel and not summoned:
        return Decision("not_intel", ex)
    sf = cfg.get("salesforce", {})
    res = resolve_extraction(source, ex, sf.get("priority_field"), sf.get("max_name_attempts", 6))
    return Decision(res.status, ex, res)
