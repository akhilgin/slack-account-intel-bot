"""Slack Web API and Salesforce REST clients on the standard library."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .matching import name_equals, name_prefix_of, norm_domain


# -- Slack ---------------------------------------------------------------------

class SlackError(Exception):
    pass


class SlackClient:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, **params):
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else v)
                for k, v in params.items() if v is not None}
        for attempt in range(4):
            req = urllib.request.Request(
                f"https://slack.com/api/{method}",
                data=urllib.parse.urlencode(data).encode(),
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(int(e.headers.get("Retry-After", "2")) + attempt)
                    continue
                raise SlackError(f"{method}: HTTP {e.code}") from e
            if not out.get("ok"):
                raise SlackError(f"{method}: {out.get('error')}")
            return out
        raise SlackError(f"{method}: rate limited")

    def paginate(self, method: str, key: str, **params):
        cursor = None
        while True:
            out = self.call(method, cursor=cursor, **params)
            yield from out.get(key, [])
            cursor = (out.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break


# -- Salesforce ----------------------------------------------------------------

class SalesforceError(Exception):
    pass


def soql_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


class SalesforceClient:
    """Credentials: SF_ACCESS_TOKEN + SF_INSTANCE_URL if set, else `sf org display` for the configured org."""

    def __init__(self, sf_cfg: dict):
        self.api_version = sf_cfg.get("api_version", "v60.0")
        self.target_org = sf_cfg.get("target_org", "production")
        self._from_env = bool(os.environ.get("SF_ACCESS_TOKEN") and os.environ.get("SF_INSTANCE_URL"))
        self.token = ""
        self.instance_url = ""
        self._refresh()

    def _refresh(self) -> None:
        if self._from_env:
            self.token, self.instance_url = os.environ["SF_ACCESS_TOKEN"], os.environ["SF_INSTANCE_URL"].rstrip("/")
            return
        r = subprocess.run(["sf", "org", "display", "--target-org", self.target_org, "--json"], capture_output=True, text=True)
        if r.returncode != 0:
            raise SalesforceError(f"sf CLI auth failed (try: sf org login web --alias {self.target_org})\n{r.stderr}")
        res = json.loads(r.stdout)["result"]
        self.token, self.instance_url = res["accessToken"], res["instanceUrl"].rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None):
        url = path if path.startswith("http") else f"{self.instance_url}/services/data/{self.api_version}/{path}"
        for attempt in range(2):
            req = urllib.request.Request(
                url, method=method, data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0 and not self._from_env:
                    self._refresh()
                    continue
                raise SalesforceError(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:500]}") from e
        raise SalesforceError("unreachable")

    def query(self, soql: str) -> list[dict]:
        out = self.request("GET", "query?" + urllib.parse.urlencode({"q": soql}))
        rows = list(out.get("records", []))
        while out.get("nextRecordsUrl"):
            out = self.request("GET", self.instance_url + out["nextRecordsUrl"])
            rows += out.get("records", [])
        return rows

    def create(self, sobject: str, fields: dict) -> str:
        return self.request("POST", f"sobjects/{sobject}", fields)["id"]

    def update(self, sobject: str, record_id: str, fields: dict) -> None:
        self.request("PATCH", f"sobjects/{sobject}/{record_id}", fields)

    def record_url(self, sobject: str, record_id: str) -> str:
        return f"{self.instance_url}/lightning/r/{sobject}/{record_id}/view"


class SalesforceAccounts:
    """Account source backed by SOQL. Field names come from config."""

    def __init__(self, client: SalesforceClient, sf_cfg: dict):
        self.c = client
        self.domain_field = sf_cfg.get("domain_field", "Website")
        self.name_fields = sf_cfg.get("name_fields") or ["Name"]
        self.priority_field = sf_cfg.get("priority_field")
        fields = ["Id", "Name", self.domain_field, *self.name_fields, "Owner.Name"]
        if self.priority_field:
            fields.append(self.priority_field)
        self.select = ", ".join(dict.fromkeys(fields))

    def _q(self, where: str) -> list[dict]:
        return self.c.query(f"SELECT {self.select} FROM Account WHERE {where} LIMIT 25")

    def by_domain(self, domain: str) -> list[dict]:
        rows = self._q(f"{self.domain_field} LIKE '%{soql_str(domain)}%'")
        return [r for r in rows if norm_domain(r.get(self.domain_field)) == domain]

    def by_name_exact(self, name: str) -> list[dict]:
        n = soql_str(name.strip())
        where = " OR ".join(f"{f} = '{n}'" for f in self.name_fields)
        return self._q(where)

    def by_name_prefix(self, name: str) -> list[dict]:
        n = soql_str(name.strip())
        where = " OR ".join(f"{f} LIKE '{n}%'" for f in self.name_fields)
        return [r for r in self._q(where) if name_prefix_of(r, name, self.name_fields) or name_equals(r, name, self.name_fields)]

    # -- helpers the bot uses besides matching --

    def find_contact(self, account_id: str, name: str | None) -> dict | None:
        if not name:
            return None
        rows = self.c.query(f"SELECT Id, Name FROM Contact WHERE AccountId = '{account_id}' AND Name = '{soql_str(name.strip())}' LIMIT 2")
        return rows[0] if len(rows) == 1 else None

    def record_type_id(self, developer_name: str, sobject: str = "Task") -> str | None:
        rows = self.c.query(
            f"SELECT Id FROM RecordType WHERE SobjectType = '{sobject}' AND DeveloperName = '{soql_str(developer_name)}' AND IsActive = true"
        )
        return rows[0]["Id"] if rows else None

    def recent_intel_tasks(self, account_id: str, task_type: str, days: int, limit: int) -> list[dict]:
        return self.c.query(
            f"SELECT Subject, ActivityDate, Description FROM Task WHERE WhatId = '{account_id}' "
            f"AND Type = '{soql_str(task_type)}' AND ActivityDate >= LAST_N_DAYS:{int(days)} "
            f"ORDER BY ActivityDate DESC, CreatedDate DESC LIMIT {int(limit)}"
        )

    def current_digest(self, account_id: str, field: str) -> str:
        rows = self.c.query(f"SELECT {field} FROM Account WHERE Id = '{account_id}'")
        return (rows[0].get(field) or "") if rows else ""
