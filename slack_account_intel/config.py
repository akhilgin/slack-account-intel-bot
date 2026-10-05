"""Config loading. Secrets come from environment variables, never from the config file.

  SLACK_BOT_TOKEN      Slack bot token (xoxb-...)
  ANTHROPIC_API_KEY    optional; enables LLM extraction and digests
  SF_ACCESS_TOKEN + SF_INSTANCE_URL
                       optional; otherwise credentials come from `sf org display`
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS: dict = {
    "bot_name": "Account Intel Bot",
    "channels": [],
    "dm_intake": True,
    "poll_interval_seconds": 60,
    "approval": "reaction",  # "reaction": propose, then thumbs-up logs. "auto": log clear matches immediately.
    "state_file": "state.json",
    "pending_ttl_days": 7,
    "confirm_reactions": ["+1", "thumbsup", "white_check_mark"],
    "reject_reactions": ["x", "-1", "thumbsdown", "no_entry_sign"],
    "choice_reactions": ["one", "two", "three", "four", "five"],
    "salesforce": {
        "target_org": "production",
        "api_version": "v60.0",
        "domain_field": "Website",
        "name_fields": ["Name"],
        "priority_field": None,
        "max_name_attempts": 6,
    },
    "task": {
        "type": "Intel",
        "subject_prefix": "Intel: ",
        "status": "Completed",
        "priority": "Normal",
        "record_type_developer_name": None,
        "fields": {"intel_type": None, "source_link": None, "logged_by": None},
        "default_owner_id": None,
        "owner_map": {},
    },
    "digest": {
        "enabled": False,
        "account_field": "Intel_Digest__c",
        "last_date_field": None,
        "max_chars": 1000,
        "max_items": 15,
        "lookback_days": 90,
    },
    "llm": {
        "enabled": True,
        "model": "claude-sonnet-5-5",
        "extract_max_tokens": 500,
        "digest_max_tokens": 600,
        "timeout_seconds": 30,
    },
    "ignore_domains": ["gmail.com", "outlook.com", "yahoo.com", "slack.com", "google.com", "linkedin.com"],
    "intel_keywords": {},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("owner_map", "intel_keywords"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict:
    """Defaults, overlaid with the JSON file at `path` if given."""
    if path is None:
        return copy.deepcopy(DEFAULTS)
    data = json.loads(Path(path).read_text())
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    return _merge(DEFAULTS, data)
