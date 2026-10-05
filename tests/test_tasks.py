import unittest

from slack_account_intel.config import load_config
from slack_account_intel.tasks import FOOTER_MARK, body_of, build_task_payload, permalink, ts_to_date

ACCT = {"Id": "001FAKE0001", "Name": "Acme Robotics"}


def payload(cfg=None, **over):
    kw = dict(account=ACCT, summary="Evaluating vendors, budget in Q3", text="Acme is evaluating vendors.",
              author="Jordan Lee", permalink="https://example.slack.com/archives/CFIXTURE1/p1760000000000100",
              channel_label="Slack #sales", date="2025-10-09", intel_type="buying_signal")
    kw.update(over)
    return build_task_payload(cfg or load_config(), **kw)


class PayloadTests(unittest.TestCase):
    def test_defaults(self):
        p = payload()
        self.assertEqual(p["Subject"], "Intel: Evaluating vendors, budget in Q3")
        self.assertEqual((p["WhatId"], p["Type"], p["Status"], p["ActivityDate"]), ("001FAKE0001", "Intel", "Completed", "2025-10-09"))
        self.assertNotIn("OwnerId", p)
        self.assertNotIn("RecordTypeId", p)

    def test_description_has_author_and_source_link(self):
        d = payload()["Description"]
        self.assertTrue(d.startswith("Acme is evaluating vendors."))
        self.assertIn("posted by Jordan Lee", d)
        self.assertIn("https://example.slack.com/archives/CFIXTURE1/p1760000000000100", d)
        self.assertEqual(body_of(d), "Acme is evaluating vendors.")

    def test_configurable_type_prefix_and_fields(self):
        cfg = load_config()
        cfg["task"].update(type="Field Intel", subject_prefix="[intel] ", default_owner_id="005FAKE",
                           fields={"intel_type": "Intel_Type__c", "source_link": "Source_Link__c", "logged_by": "Logged_By__c"})
        p = payload(cfg, contact_id="003FAKE", record_type_id="012FAKE")
        self.assertEqual(p["Type"], "Field Intel")
        self.assertTrue(p["Subject"].startswith("[intel] "))
        self.assertEqual((p["Intel_Type__c"], p["Logged_By__c"]), ("buying_signal", "Jordan Lee"))
        self.assertTrue(p["Source_Link__c"].startswith("https://example.slack.com/"))
        self.assertEqual((p["OwnerId"], p["WhoId"], p["RecordTypeId"]), ("005FAKE", "003FAKE", "012FAKE"))

    def test_owner_map_beats_default(self):
        cfg = load_config()
        cfg["task"].update(default_owner_id="005DEFAULT", owner_map={"Jordan Lee": "005JORDAN"})
        self.assertEqual(payload(cfg)["OwnerId"], "005JORDAN")
        self.assertEqual(payload(cfg, author="Sam Rivera")["OwnerId"], "005DEFAULT")

    def test_subject_truncated(self):
        self.assertEqual(len(payload(summary="x" * 400)["Subject"]), 255)

    def test_forwarded_note(self):
        self.assertIn("forwarded from Sam Rivera", payload(forwarded_from=["Sam Rivera"])["Description"])

    def test_permalink_and_date(self):
        self.assertEqual(permalink("https://t.example/", "C1", "1760000000.000100"), "https://t.example/archives/C1/p1760000000000100")
        self.assertIn("thread_ts=1.2&cid=C1", permalink("https://t.example", "C1", "3.4", "1.2"))
        self.assertEqual(ts_to_date("1760000000.0"), "2025-10-09")
        self.assertIn(FOOTER_MARK, payload()["Description"])


if __name__ == "__main__":
    unittest.main()
