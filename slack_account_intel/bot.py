"""Live bot: poll Slack, find intel, propose or log Salesforce Tasks.

Polling (conversations.history) rather than Socket Mode or Events API: no public endpoint and no
always-on listener are needed. Approval is by emoji reaction, which the next poll reads.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .clients import SlackError
from .digest import make_digest
from .pipeline import Decision, analyze
from .tasks import build_task_payload, permalink, ts_to_date


def ts6(x) -> str:
    return f"{float(x):.6f}"  # Slack rejects more than 6 decimals


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"last_ts": {}, "pending": {}, "logged": {}, "skipped": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


class Bot:
    def __init__(self, cfg: dict, slack, sf, accounts, llm, write: bool = True, log=print):
        self.cfg, self.slack, self.sf, self.accounts, self.llm = cfg, slack, sf, accounts, llm
        self.write = write
        self.log = log
        self.state = State(cfg["state_file"])
        self.bot_id = ""
        self.team_url = ""
        self._names: dict[str, str] = {}
        self._rt_id: str | None = None

    # -- Slack helpers -----------------------------------------------------

    def display_name(self, user_id: str | None) -> str:
        if not user_id:
            return "unknown"
        if user_id not in self._names:
            try:
                p = self.slack.call("users.info", user=user_id)["user"]
                prof = p.get("profile", {})
                self._names[user_id] = prof.get("real_name") or prof.get("display_name") or p.get("name") or user_id
            except SlackError:
                self._names[user_id] = user_id
        return self._names[user_id]

    def humanize(self, text: str) -> str:
        text = re.sub(r"<@([UW][A-Z0-9]+)>", lambda m: "@" + self.display_name(m.group(1)), text)
        text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

    def is_human(self, m: dict) -> bool:
        if m.get("bot_id") or m.get("subtype") not in (None, "thread_broadcast") or m.get("user") == self.bot_id:
            return False
        return bool((m.get("text") or "").strip())

    def post(self, channel: str, thread_ts: str, text: str) -> str | None:
        if not self.write:
            self.log(f"  [no-write] would reply in thread: {text.splitlines()[0]}")
            return None
        return self.slack.call("chat.postMessage", channel=channel, thread_ts=thread_ts, text=text,
                               unfurl_links=False, unfurl_media=False)["ts"]

    # -- channel discovery and fetching -----------------------------------------

    def discover(self) -> dict[str, tuple[str, str]]:
        """channel id -> (name, mode). Modes: passive (read everything), mention (only @mentions), dm."""
        out: dict[str, tuple[str, str]] = {}
        configured = set(self.cfg["channels"])
        for ch in self.slack.paginate("conversations.list", "channels", types="public_channel,private_channel",
                                      exclude_archived=True, limit=200):
            if ch["name"] in configured:
                if not ch.get("is_member") and not ch.get("is_private") and self.write:
                    self.slack.call("conversations.join", channel=ch["id"])
                out[ch["id"]] = (ch["name"], "passive")
            elif ch.get("is_member"):
                out[ch["id"]] = (ch["name"], "mention")
        if self.cfg["dm_intake"]:
            for ch in self.slack.paginate("conversations.list", "channels", types="im", exclude_archived=True, limit=200):
                if ch.get("user") and ch["user"] != "USLACKBOT":
                    out[ch["id"]] = (f"dm-{self.display_name(ch['user'])}", "dm")
        return out

    def fetch(self, channel: str, oldest: str) -> tuple[list[dict], dict[str, list[dict]]]:
        """New human messages (top-level and thread replies) plus the threads they sit in."""
        top = list(self.slack.paginate("conversations.history", "messages", channel=channel, oldest=ts6(oldest), limit=200))
        older = self.slack.call("conversations.history", channel=channel, oldest=ts6(float(oldest) - 7 * 86400),
                                latest=ts6(oldest), limit=200).get("messages", [])
        parents = [m for m in top + older if m.get("reply_count") and float(m.get("latest_reply", "0")) > float(oldest)]
        threads: dict[str, list[dict]] = {}
        new = [m for m in top if self.is_human(m)]
        for p in parents:
            reps = list(self.slack.paginate("conversations.replies", "messages", channel=channel, ts=p["ts"], limit=200))
            threads[p["ts"]] = reps
            for r in reps:
                if r["ts"] != p["ts"] and float(r["ts"]) > float(oldest) and self.is_human(r):
                    r["_thread_ts"] = p["ts"]
                    new.append(r)
        seen, uniq = set(), []
        for m in sorted(new, key=lambda m: float(m["ts"])):
            if m["ts"] not in seen:
                seen.add(m["ts"])
                uniq.append(m)
        return uniq, threads

    def reactions_on(self, channel: str, thread_ts: str, msg_ts: str) -> set[str]:
        for m in self.slack.paginate("conversations.replies", "messages", channel=channel, ts=thread_ts, limit=200):
            if m["ts"] == msg_ts:
                return {rx["name"].split("::")[0] for rx in m.get("reactions", []) if any(u != self.bot_id for u in rx.get("users", []))}
        return set()

    # -- Salesforce actions -----------------------------------------------------

    def record_type_id(self) -> str | None:
        dev = self.cfg["task"].get("record_type_developer_name")
        if not dev:
            return None
        if self._rt_id is None:
            self._rt_id = self.accounts.record_type_id(dev) or ""
            if not self._rt_id:
                self.log(f"warning: Task record type '{dev}' not found; writing plain Tasks")
        return self._rt_id or None

    def log_task(self, item: dict) -> str | None:
        contact = self.accounts.find_contact(item["account"]["Id"], item.get("contact_name"))
        payload = build_task_payload(
            self.cfg, account=item["account"], summary=item["summary"], text=item["text"], author=item["author"],
            permalink=item["permalink"], channel_label=item["channel_label"], date=item["date"],
            intel_type=item["intel_type"], contact_id=contact["Id"] if contact else None,
            record_type_id=self.record_type_id(),
        )
        if not self.write:
            self.log("  [no-write] would create Task on " + item["account"]["Name"] + ":\n" + json.dumps(payload, indent=2))
            return None
        task_id = self.sf.create("Task", payload)
        self.log(f"  logged Task {task_id} on {item['account']['Name']}")
        if self.cfg["digest"]["enabled"]:
            self.refresh_digest(item["account"])
        return task_id

    def refresh_digest(self, account: dict) -> None:
        d = self.cfg["digest"]
        try:
            tasks = self.accounts.recent_intel_tasks(account["Id"], self.cfg["task"]["type"], d["lookback_days"], d["max_items"])
            previous = self.accounts.current_digest(account["Id"], d["account_field"])
            digest = make_digest(self.llm, account["Name"], tasks, previous, self.cfg)
            fields = {d["account_field"]: digest.text}
            if d.get("last_date_field"):
                fields[d["last_date_field"]] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.sf.update("Account", account["Id"], fields)
            self.log(f"  digest updated on {account['Name']} ({digest.source})")
        except Exception as e:  # a failed digest must never undo a logged Task
            self.log(f"  digest update failed for {account['Name']}: {e}")

    # -- proposals ---------------------------------------------------------

    def account_link(self, a: dict) -> str:
        return f"<{self.sf.record_url('Account', a['Id'])}|{a['Name']}>"

    def proposal_text(self, item: dict) -> str:
        return (f":mag: Looks like intel about *{self.account_link(item['account'])}*\n*{item['summary']}*\n"
                f"React :+1: to log this on the account in Salesforce, or :x: to skip.")

    def choose_text(self, name: str, cands: list[dict]) -> str:
        reacts = self.cfg["choice_reactions"]
        lines = [f":{reacts[i]}: {c['Name']}" for i, c in enumerate(cands[: len(reacts)])]
        return (f":mag: Looks like intel about *{name}*, but more than one Salesforce account matches. "
                "React with the right number to log it there, or :x: to skip.\n" + "\n".join(lines))

    def act(self, item: dict, d: Decision, chan: str, thread_ts: str, keys: list[str]) -> None:
        st = self.state.data
        if d.status == "ambiguous":
            cands = [{"Id": c["Id"], "Name": c["Name"]} for c in d.resolution.candidates]
            pts = self.post(chan, thread_ts, self.choose_text(d.extraction.company_name or "this company", cands))
            if pts:
                st["pending"][pts] = {**item, "kind": "choose", "channel": chan, "thread_ts": thread_ts, "keys": keys,
                                      "candidates": cands, "posted_at": time.time()}
            return
        a = d.resolution.account
        item["account"] = {"Id": a["Id"], "Name": a["Name"]}
        # Auto-log only on a domain or exact-name match; a prefix match still asks first.
        if self.cfg["approval"] == "auto" and d.method in ("domain", "exact"):
            task_id = self.log_task(item)
            if task_id:
                self.post(chan, thread_ts, f":white_check_mark: Logged on *{self.account_link(item['account'])}*: "
                          f"<{self.sf.record_url('Task', task_id)}|{item['summary']}>")
                for k in keys:
                    st["logged"][k] = task_id
            return
        pts = self.post(chan, thread_ts, self.proposal_text(item))
        if pts:
            st["pending"][pts] = {**item, "kind": "proposal", "channel": chan, "thread_ts": thread_ts, "keys": keys,
                                  "posted_at": time.time()}
        else:
            self.log_task(item)  # no-write: show the Task that would be created

    def check_pending(self) -> None:
        st = self.state.data
        for pts, p in list(st["pending"].items()):
            try:
                names = self.reactions_on(p["channel"], p["thread_ts"], pts)
            except SlackError as e:
                self.log(f"reaction check failed: {e}")
                continue
            done = False
            if names & set(self.cfg["reject_reactions"]):
                for k in p["keys"]:
                    st["skipped"][k] = "rejected"
                done = True
            elif p["kind"] == "proposal" and names & set(self.cfg["confirm_reactions"]):
                done = self._confirm(p)
            elif p["kind"] == "choose":
                for i, r in enumerate(self.cfg["choice_reactions"][: len(p["candidates"])]):
                    if r in names:
                        p["account"] = p["candidates"][i]
                        done = self._confirm(p)
                        break
            if not done and time.time() - p["posted_at"] > self.cfg["pending_ttl_days"] * 86400:
                for k in p["keys"]:
                    st["skipped"][k] = "expired"
                done = True
            if done:
                del st["pending"][pts]

    def _confirm(self, p: dict) -> bool:
        task_id = self.log_task(p)
        if not task_id:
            return False
        self.post(p["channel"], p["thread_ts"], f":white_check_mark: Logged on *{self.account_link(p['account'])}*: "
                  f"<{self.sf.record_url('Task', task_id)}|{p['summary']}>")
        for k in p["keys"]:
            self.state.data["logged"][k] = task_id
        return True

    # -- main pass ---------------------------------------------------------

    def start(self) -> None:
        auth = self.slack.call("auth.test")
        self.bot_id, self.team_url = auth["user_id"], auth["url"]
        self.log(f"{self.cfg['bot_name']} running as {auth['user']} (write={'on' if self.write else 'off'})")

    def run_pass(self, since_hours: float) -> None:
        st = self.state.data
        if self.write:
            self.check_pending()
        mention = f"<@{self.bot_id}>"
        for chan, (cname, mode) in self.discover().items():
            default_oldest = time.time() - since_hours * 3600
            oldest = st["last_ts"].get(chan, ts6(default_oldest)) if self.write else ts6(default_oldest)
            try:
                new, threads = self.fetch(chan, oldest)
            except SlackError as e:
                self.log(f"#{cname}: {e}")
                continue
            newest = oldest
            for m in new:
                newest = max(newest, m["ts"], key=float)
                key = f"{chan}:{m['ts']}"
                summoned = mention in m.get("text", "")
                if key in st["logged"] or key in st["skipped"] or any(key in p["keys"] for p in st["pending"].values()):
                    continue
                if mode == "mention" and not summoned:
                    continue
                tts = m.get("_thread_ts") or m.get("thread_ts") or m["ts"]
                parts = [m]
                if summoned and m.get("_thread_ts"):  # mentioned inside a thread: the thread so far is the material
                    parts = [x for x in threads.get(tts, []) if float(x["ts"]) <= float(m["ts"]) and self.is_human(x)] or [m]
                text = "\n".join(self.humanize(x["text"]).replace(mention, "").strip() for x in parts).strip()
                author = self.display_name(m.get("user"))
                d = analyze(text, self.cfg, self.accounts, self.llm, summoned)
                if d.status in ("none", "not_intel"):
                    if summoned:
                        self.post(chan, tts, ":thinking_face: I could not tell which Salesforce account this is about. "
                                  "Reply with the account name and the fact and I will try again.")
                    st["skipped"][key] = d.status
                    continue
                item = {
                    "summary": d.extraction.summary, "text": text, "author": author, "intel_type": d.extraction.intel_type,
                    "contact_name": d.extraction.contact_name, "date": ts_to_date(parts[0]["ts"]),
                    "channel_label": "a direct message" if mode == "dm" else f"Slack #{cname}",
                    "permalink": permalink(self.team_url, chan, parts[0]["ts"], parts[0].get("_thread_ts")),
                }
                self.log(f"#{cname}: {d.status} ({d.method}) {d.extraction.summary}")
                self.act(item, d, chan, tts, [f"{chan}:{x['ts']}" for x in parts])
            if self.write:
                st["last_ts"][chan] = newest
        if self.write:
            self.state.save()

    def run(self, once: bool, since_hours: float) -> None:
        self.start()
        while True:
            try:
                self.run_pass(since_hours)
            except SlackError as e:
                self.log(f"Slack error: {e}")
            if once:
                break
            time.sleep(self.cfg["poll_interval_seconds"])
