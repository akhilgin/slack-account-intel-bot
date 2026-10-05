import json
import unittest
from pathlib import Path

from slack_account_intel.matching import ListAccountSource, norm_domain, norm_name, resolve, resolve_extraction
from slack_account_intel.extract import Extraction

ACCOUNTS = json.loads((Path(__file__).parent.parent / "fixtures" / "accounts.json").read_text())


def source(accounts=None):
    return ListAccountSource(accounts or ACCOUNTS)


class NormTests(unittest.TestCase):
    def test_domain_forms(self):
        self.assertEqual(norm_domain("https://www.Acme-Robotics.example/about?x=1"), "acme-robotics.example")
        self.assertEqual(norm_domain("dana@Northwind-Analytics.example"), "northwind-analytics.example")
        self.assertEqual(norm_domain(None), "")

    def test_name_drops_suffix_and_punctuation(self):
        self.assertEqual(norm_name("Northwind Analytics, Inc."), "northwind analytics")
        self.assertEqual(norm_name("Globex Corporation"), "globex")


class ResolveTests(unittest.TestCase):
    def test_domain_match_wins_over_name(self):
        r = resolve(source(), name="Globex", domain="https://www.acme-robotics.example")
        self.assertEqual((r.status, r.method, r.account["Name"]), ("match", "domain", "Acme Robotics"))

    def test_email_domain_matches(self):
        r = resolve(source(), domain="dana@northwind-analytics.example")
        self.assertEqual(r.account["Id"], "001FAKE0002")

    def test_exact_name_ignores_suffix(self):
        r = resolve(source(), name="Northwind Analytics")
        self.assertEqual((r.status, r.method), ("match", "exact"))

    def test_prefix_unique(self):
        r = resolve(source(), name="Acme")
        self.assertEqual((r.status, r.method, r.account["Name"]), ("match", "prefix", "Acme Robotics"))

    def test_prefix_is_whole_word(self):
        self.assertEqual(resolve(source(), name="Acm").status, "none")

    def test_ambiguous_prefix(self):
        r = resolve(source(), name="Globex")
        self.assertEqual(r.status, "ambiguous")
        self.assertEqual({c["Name"] for c in r.candidates}, {"Globex Energy", "Globex Logistics"})
        self.assertIsNone(r.account)

    def test_priority_field_breaks_tie(self):
        accts = [dict(a) for a in ACCOUNTS]
        accts[3]["Is_Target__c"] = True
        r = resolve(source(accts), name="Globex", priority_field="Is_Target__c")
        self.assertEqual((r.status, r.account["Name"]), ("match", "Globex Logistics"))

    def test_two_priority_accounts_stay_ambiguous(self):
        accts = [dict(a, Is_Target__c=True) for a in ACCOUNTS]
        self.assertEqual(resolve(source(accts), name="Globex", priority_field="Is_Target__c").status, "ambiguous")

    def test_no_match(self):
        self.assertEqual(resolve(source(), name="Umbrella Labs").status, "none")

    def test_extraction_tries_candidates_in_order(self):
        ex = Extraction(company_name="Budget", candidates=["Budget", "Initech"])
        r = resolve_extraction(source(), ex)
        self.assertEqual(r.account["Name"], "Initech")

    def test_extraction_domain_first(self):
        ex = Extraction(company_name="Initech", domains=["globex.example"], candidates=["Initech"])
        self.assertEqual(resolve_extraction(source(), ex).account["Name"], "Globex Energy")


if __name__ == "__main__":
    unittest.main()
