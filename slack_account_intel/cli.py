"""Command line entry point.

  python -m slack_account_intel --offline                 # fixtures only, no network at all
  python -m slack_account_intel --config config.json --no-write --since 24
  python -m slack_account_intel --config config.json --once
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import load_config
from .digest import build_digest_prompt, make_digest
from .matching import ListAccountSource
from .pipeline import analyze
from .tasks import build_task_payload, ts_to_date

ROOT = Path(__file__).resolve().parent.parent


def make_llm(cfg: dict):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not cfg["llm"]["enabled"] or not key:
        return None
    from .llm import AnthropicClient

    return AnthropicClient(key, cfg["llm"]["model"], cfg["llm"]["timeout_seconds"])


def run_offline(cfg: dict, fixtures: Path) -> None:
    """Exercise parsing, matching, payload building and the digest fallback on fixture data. No network."""
    sf = cfg["salesforce"]
    accounts = json.loads((fixtures / "accounts.json").read_text())
    source = ListAccountSource(accounts, sf["domain_field"], sf["name_fields"])
    messages = json.loads((fixtures / "messages.json").read_text())
    print(f"offline mode: {len(messages)} fixture messages, {len(accounts)} fixture accounts, LLM off\n")
    for m in messages:
        d = analyze(m["text"], cfg, source, llm=None, summoned=m.get("mention", False))
        print(f"[{m['author']} in #{m['channel']}] {m['text']}")
        print(f"  extracted: company={d.extraction.company_name!r} domains={d.extraction.domains} type={d.extraction.intel_type}")
        if d.status == "match":
            a = d.resolution.account
            print(f"  account:   MATCH {a['Name']} via {d.method}")
            payload = build_task_payload(
                cfg, account=a, summary=d.extraction.summary, text=m["text"], author=m["author"],
                permalink=m["permalink"], channel_label=f"Slack #{m['channel']}", date=ts_to_date(m["ts"]),
                intel_type=d.extraction.intel_type,
            )
            print("  would create Task:")
            print("    " + json.dumps(payload, indent=2).replace("\n", "\n    "))
        elif d.status == "ambiguous":
            print("  account:   AMBIGUOUS, the bot would ask the author to pick:")
            for i, c in enumerate(d.resolution.candidates, 1):
                print(f"    {i}. {c['Name']}")
        else:
            print(f"  account:   {d.status.upper()}, nothing would be logged")
        print()
    tasks_file = fixtures / "intel_tasks.json"
    if tasks_file.exists():
        tasks = json.loads(tasks_file.read_text())
        dg = cfg["digest"]
        _, user = build_digest_prompt("Acme Robotics", tasks, "", dg["max_chars"], dg["max_items"])
        print("digest prompt (user part) for Acme Robotics:\n" + user + "\n")
        print("digest with no LLM (fallback):\n" + make_digest(None, "Acme Robotics", tasks, "", cfg).text)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Slack account intel bot")
    ap.add_argument("--config", help="path to config.json (see config.example.json)")
    ap.add_argument("--offline", action="store_true", help="run on fixtures with no network")
    ap.add_argument("--fixtures", default=str(ROOT / "fixtures"), help="fixture directory for --offline")
    ap.add_argument("--no-write", action="store_true",
                    help="read Slack and Salesforce, but post nothing and print the Tasks that would be created")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--since", type=float, default=24, help="hours to look back on the first pass (default 24)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.offline:
        run_offline(cfg, Path(args.fixtures))
        return

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        sys.exit("SLACK_BOT_TOKEN is not set")
    from .bot import Bot
    from .clients import SalesforceAccounts, SalesforceClient, SlackClient

    sf = SalesforceClient(cfg["salesforce"])
    bot = Bot(cfg, SlackClient(token), sf, SalesforceAccounts(sf, cfg["salesforce"]), make_llm(cfg), write=not args.no_write)
    bot.run(once=args.once or args.no_write, since_hours=args.since)
