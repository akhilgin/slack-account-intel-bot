"""Rolling intel digest for an Account: LLM summary with a deterministic fallback."""

from __future__ import annotations

from dataclasses import dataclass

from .llm import LLMError
from .tasks import body_of

DIGEST_SYSTEM = """You maintain a short "intel digest" on a CRM account record. You get the account name, the current digest (may be empty) and recent intel entries, newest first.

Write the updated digest:
- plain text, one bullet per line starting with "- ", newest and most useful first
- merge duplicates; drop facts that a newer entry contradicts
- keep names, dates and numbers exactly as written; never add facts that are not in the entries
- stay under the character limit given
Return only the digest text."""


@dataclass
class Digest:
    text: str
    source: str  # "llm" | "fallback"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;-") + "..."


def _entry_lines(tasks: list[dict], max_items: int, with_body: bool) -> list[str]:
    ordered = sorted(tasks, key=lambda t: t.get("ActivityDate") or "", reverse=True)[:max_items]
    lines = []
    for t in ordered:
        subject = (t.get("Subject") or "").removeprefix("Intel: ").strip()
        line = f"{t.get('ActivityDate') or 'undated'}: {subject}"
        if with_body:
            body = " ".join(body_of(t.get("Description") or "").split())
            if body and body != subject:
                line += f" | detail: {body[:300]}"
        lines.append(line)
    return lines


def build_digest_prompt(account_name: str, tasks: list[dict], previous: str, max_chars: int, max_items: int) -> tuple[str, str]:
    entries = "\n".join(f"- {line}" for line in _entry_lines(tasks, max_items, with_body=True)) or "(none)"
    user = (
        f"Account: {account_name}\n"
        f"Character limit: {max_chars}\n\n"
        f"Current digest:\n{previous.strip() or '(empty)'}\n\n"
        f"Recent intel entries, newest first:\n{entries}"
    )
    return DIGEST_SYSTEM, user


def fallback_digest(tasks: list[dict], max_chars: int, max_items: int) -> str:
    """No model: newest entries as bullets, cut to fit."""
    lines = [f"- {line}" for line in _entry_lines(tasks, max_items, with_body=False)]
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) if out else truncate(lines[0], max_chars) if lines else ""


def make_digest(llm, account_name: str, tasks: list[dict], previous: str, cfg: dict) -> Digest:
    d = cfg["digest"]
    if not tasks:
        return Digest(truncate(previous, d["max_chars"]), "fallback")
    if llm is not None:
        system, user = build_digest_prompt(account_name, tasks, previous, d["max_chars"], d["max_items"])
        try:
            text = llm.complete(system, user, cfg["llm"]["digest_max_tokens"])
            if text.strip():
                return Digest(truncate(text, d["max_chars"]), "llm")
        except LLMError:
            pass
    return Digest(fallback_digest(tasks, d["max_chars"], d["max_items"]), "fallback")
