"""Build the Salesforce Task payload for a piece of intel."""

from __future__ import annotations

from datetime import datetime, timezone

FOOTER_MARK = "\n\n-- Logged via"


def ts_to_date(ts: str | float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def permalink(team_url: str, channel: str, ts: str, thread_ts: str | None = None) -> str:
    base = f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        base += f"?thread_ts={thread_ts}&cid={channel}"
    return base


def body_of(description: str) -> str:
    """The message text of a Task description, without the bot's footer."""
    return (description or "").split(FOOTER_MARK, 1)[0].strip()


def build_task_payload(
    cfg: dict,
    *,
    account: dict,
    summary: str,
    text: str,
    author: str,
    permalink: str,
    channel_label: str,
    date: str,
    intel_type: str = "other",
    contact_id: str | None = None,
    record_type_id: str | None = None,
    forwarded_from: list[str] | None = None,
) -> dict:
    t = cfg["task"]
    footer = f"{FOOTER_MARK} {cfg.get('bot_name', 'the intel bot')} from {channel_label} ({intel_type}), posted by {author}"
    if forwarded_from:
        footer += f", forwarded from {', '.join(forwarded_from)}"
    footer += f"\n{permalink}"
    payload: dict = {
        "Subject": f"{t['subject_prefix']}{summary}"[:255],
        "Description": (text.strip() + footer)[:32000],
        "WhatId": account["Id"],
        "Status": t["status"],
        "Priority": t["priority"],
        "ActivityDate": date,
        "Type": t["type"],
    }
    owner = t.get("owner_map", {}).get(author) or t.get("default_owner_id")
    if owner:
        payload["OwnerId"] = owner
    if contact_id:
        payload["WhoId"] = contact_id
    if record_type_id:
        payload["RecordTypeId"] = record_type_id
    fields = t.get("fields") or {}
    if fields.get("intel_type"):
        payload[fields["intel_type"]] = intel_type
    if fields.get("source_link"):
        payload[fields["source_link"]] = permalink[:255]
    if fields.get("logged_by"):
        payload[fields["logged_by"]] = author[:255]
    return payload
