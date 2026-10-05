"""Account matching: domain first, then exact name, then name prefix. Pure logic, no network.

An account source is anything with three methods returning lists of account dicts:
  by_domain(domain), by_name_exact(name), by_name_prefix(name)
`ListAccountSource` serves fixtures and tests; `SalesforceAccounts` (clients.py) serves the real org.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|plc|sa|ag)\b\.?", re.I)


def norm_domain(d: str | None) -> str:
    d = (d or "").strip().lower()
    if not d:
        return ""
    if "@" in d:
        d = d.split("@")[-1]
    d = d.split("//")[-1].split("/")[0].split("?")[0]
    return d[4:] if d.startswith("www.") else d


def norm_name(n: str | None) -> str:
    n = _SUFFIX_RE.sub(" ", (n or "").lower())
    n = re.sub(r"[^a-z0-9& ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def name_equals(rec: dict, name: str, name_fields: list[str]) -> bool:
    target = norm_name(name)
    return bool(target) and any(norm_name(rec.get(f)) == target for f in name_fields)


def name_prefix_of(rec: dict, name: str, name_fields: list[str]) -> bool:
    """True if `name` is a whole-word prefix of one of the record's names ("Acme" -> "Acme Robotics")."""
    target = norm_name(name)
    if not target:
        return False
    for f in name_fields:
        n = norm_name(rec.get(f))
        if n == target or n.startswith(target + " "):
            return True
    return False


@dataclass
class Resolution:
    status: str  # "match" | "ambiguous" | "none"
    method: str | None = None  # "domain" | "exact" | "prefix"
    account: dict | None = None
    candidates: list[dict] = field(default_factory=list)


class ListAccountSource:
    def __init__(self, accounts: list[dict], domain_field: str = "Website", name_fields: list[str] | None = None):
        self.accounts = accounts
        self.domain_field = domain_field
        self.name_fields = name_fields or ["Name"]

    def by_domain(self, domain: str) -> list[dict]:
        d = norm_domain(domain)
        return [a for a in self.accounts if d and norm_domain(a.get(self.domain_field)) == d]

    def by_name_exact(self, name: str) -> list[dict]:
        return [a for a in self.accounts if name_equals(a, name, self.name_fields)]

    def by_name_prefix(self, name: str) -> list[dict]:
        return [a for a in self.accounts if name_prefix_of(a, name, self.name_fields)]


def _rank_key(priority_field: str | None):
    def key(a: dict):
        return (0 if priority_field and a.get(priority_field) else 1, (a.get("Name") or "").lower())

    return key


def resolve(source, name: str | None = None, domain: str | None = None, priority_field: str | None = None) -> Resolution:
    """Find the account for a name and/or domain.

    A single hit is a match. With several hits, a lone priority account (if `priority_field` is set)
    wins; otherwise the result is ambiguous and a human picks.
    """
    cands: list[dict] = []
    method = None
    if domain:
        cands, method = source.by_domain(norm_domain(domain)), "domain"
    if not cands and name:
        cands, method = source.by_name_exact(name), "exact"
        if not cands:
            cands, method = source.by_name_prefix(name), "prefix"
    if not cands:
        return Resolution("none")
    cands = sorted({c["Id"]: c for c in cands}.values(), key=_rank_key(priority_field))
    if len(cands) == 1:
        return Resolution("match", method, cands[0], cands)
    prio = [c for c in cands if priority_field and c.get(priority_field)]
    if len(prio) == 1:
        return Resolution("match", method, prio[0], cands[:5])
    return Resolution("ambiguous", method, None, cands[:5])


def resolve_extraction(source, ex, priority_field: str | None = None, max_name_attempts: int = 6) -> Resolution:
    """Try the extraction's domains, then its candidate names. First non-empty result wins."""
    for d in ex.domains or ([ex.company_domain] if ex.company_domain else []):
        r = resolve(source, domain=d, priority_field=priority_field)
        if r.status != "none":
            return r
    names: list[str] = []
    for n in [ex.company_name, *ex.candidates]:
        if n and n not in names:
            names.append(n)
    for n in names[:max_name_attempts]:
        r = resolve(source, name=n, priority_field=priority_field)
        if r.status != "none":
            return r
    return Resolution("none")
