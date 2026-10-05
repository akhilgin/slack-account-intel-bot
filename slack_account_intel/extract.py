"""Turn a Slack message into a candidate account reference plus a one-line summary.

Two paths: an LLM path (if configured) and a keyword/regex path that always works offline.
If the LLM path fails for any reason, the keyword path runs instead.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from .llm import LLMError, parse_json_object

INTEL_TYPES = [
    "competitor_or_stack",
    "people_or_org",
    "pain_or_priority",
    "buying_signal",
    "relationship",
    "deal_context",
    "other",
]

STOPWORDS = {
    "i", "we", "they", "he", "she", "it", "the", "a", "an", "this", "that", "these", "those", "our", "their",
    "my", "his", "her", "its", "and", "but", "or", "so", "if", "also", "just", "fyi", "note", "update", "heads",
    "hey", "hi", "hello", "thanks", "thank", "ok", "yes", "no", "today", "tomorrow", "yesterday", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october", "november", "december", "slack", "intel", "account",
}
FILE_EXTS = {"pdf", "png", "jpg", "jpeg", "gif", "doc", "docx", "csv", "txt", "xlsx", "pptx", "md", "json"}
_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9&'\-]*(?:\.[A-Za-z0-9]+)?(?:\s+[A-Z][A-Za-z0-9&'\-]*){0,2}")
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b")
_QUARTER_RE = re.compile(r"^Q[1-4]$")


@dataclass
class Extraction:
    company_name: str | None = None
    company_domain: str | None = None
    domains: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    contact_name: str | None = None
    intel_type: str = "other"
    summary: str = ""
    is_intel: bool = True
    source: str = "heuristic"


def clean_text(text: str) -> str:
    """Strip Slack markup so regexes see plain text."""
    t = re.sub(r"<mailto:([^|>]+)(?:\|[^>]*)?>", r"\1", text or "")
    t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 \1", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    t = re.sub(r"<@[A-Z0-9]+(?:\|[^>]*)?>", "", t)
    t = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", t)
    return html.unescape(t).strip()


def find_domains(text: str, ignore: list[str] | None = None) -> list[str]:
    ignore_set = {d.lower() for d in (ignore or [])}
    out: list[str] = []
    for m in _DOMAIN_RE.finditer(clean_text(text).lower()):
        d = m.group(0)
        d = d[4:] if d.startswith("www.") else d
        if d.rsplit(".", 1)[-1] in FILE_EXTS or d in ignore_set or d in out:
            continue
        out.append(d)
    return out


def name_candidates(text: str, limit: int = 6) -> list[str]:
    """Capitalized word runs that might be company names, in order of appearance."""
    t = _DOMAIN_RE.sub(" ", clean_text(text))  # domains are handled separately
    out: list[str] = []
    for m in _NAME_RE.finditer(t):
        words = m.group(0).split()
        while words and (words[0].lower() in STOPWORDS or _QUARTER_RE.match(words[0])):
            words.pop(0)
        while words and (words[-1].lower() in STOPWORDS or _QUARTER_RE.match(words[-1])):
            words.pop()
        if not words:
            continue
        name = " ".join(words).strip(" .,-")
        if len(name) < 2 or name in out:
            continue
        out.append(name)
        if len(out) >= limit:
            break
    # leading "Name: fact" form is the strongest hint; put it first
    lead = re.match(r"\s*([A-Z][\w&'\- ]{1,40}?)\s*[:—]\s+\S", clean_text(text))
    if lead and len(lead.group(1).split()) <= 4 and lead.group(1).split()[0].lower() not in STOPWORDS:
        name = lead.group(1).strip()
        if name in out:
            out.remove(name)
        out.insert(0, name)
    return out[:limit]


def classify_type(text: str, keywords: dict[str, list[str]]) -> str:
    low = (text or "").lower()
    best, best_hits = "other", 0
    for itype, kws in keywords.items():
        hits = sum(1 for kw in kws if re.search(rf"\b{re.escape(kw.lower())}\b", low))
        if hits > best_hits:
            best, best_hits = itype, hits
    return best


def summarize(text: str, limit: int = 120) -> str:
    first = ""
    for part in re.split(r"(?<=[.!?])\s+|\n", clean_text(text)):
        if part.strip():
            first = part.strip()
            break
    first = re.sub(r"\s+", " ", first)
    return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."


def heuristic_extract(text: str, cfg: dict, source: str = "heuristic") -> Extraction:
    cands = name_candidates(text)
    doms = find_domains(text, cfg.get("ignore_domains"))
    return Extraction(
        company_name=cands[0] if cands else None,
        company_domain=doms[0] if doms else None,
        domains=doms,
        candidates=cands,
        intel_type=classify_type(text, cfg.get("intel_keywords") or {}),
        summary=summarize(text),
        source=source,
    )


EXTRACT_SYSTEM = f"""You read a message from a sales or marketing team's Slack and decide whether it contains account intel: a fact about one specific external company (a prospect, customer or lost deal) that a seller would want on that company's CRM record later.

Return only a JSON object with these keys:
- is_intel: true or false
- company_name: the external company, or null if it cannot be determined
- company_domain: a domain that appears in the text (email address or URL), else null. Never guess one.
- contact_name: a named person at that company, else null
- intel_type: one of {", ".join(INTEL_TYPES)}
- summary: one line under 120 characters, written as a fact about the company

Rules: copy names and dates as written. Internal scheduling or routing chatter is not intel. Only report facts that are in the message."""


def llm_extract(llm, text: str, cfg: dict) -> Extraction:
    raw = llm.complete(EXTRACT_SYSTEM, clean_text(text), cfg.get("llm", {}).get("extract_max_tokens", 500))
    obj = parse_json_object(raw)
    name = (obj.get("company_name") or "").strip() or None
    dom = (obj.get("company_domain") or "").strip() or None
    itype = obj.get("intel_type") if obj.get("intel_type") in INTEL_TYPES else "other"
    summary = str(obj.get("summary") or "").strip() or summarize(text)
    doms = find_domains(text, cfg.get("ignore_domains"))
    if dom and dom not in doms:
        doms.insert(0, dom)
    return Extraction(
        company_name=name,
        company_domain=dom,
        domains=doms,
        candidates=[name] if name else [],
        contact_name=(obj.get("contact_name") or None),
        intel_type=itype,
        summary=summary[:200],
        is_intel=bool(obj.get("is_intel", True)),
        source="llm",
    )


def extract(text: str, cfg: dict, llm=None) -> Extraction:
    """LLM extraction when a client is given; keyword extraction if there is none or it fails."""
    if llm is not None:
        try:
            return llm_extract(llm, text, cfg)
        except (LLMError, ValueError, TypeError, KeyError, AttributeError):
            return heuristic_extract(text, cfg, source="heuristic-fallback")
    return heuristic_extract(text, cfg)
