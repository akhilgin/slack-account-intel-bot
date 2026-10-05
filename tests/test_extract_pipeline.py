import json
import unittest
from pathlib import Path

from slack_account_intel.config import load_config
from slack_account_intel.extract import clean_text, extract, find_domains, name_candidates, summarize
from slack_account_intel.llm import LLMError
from slack_account_intel.matching import ListAccountSource
from slack_account_intel.pipeline import analyze

FIX = Path(__file__).parent.parent / "fixtures"
SOURCE = ListAccountSource(json.loads((FIX / "accounts.json").read_text()))
CFG = load_config(Path(__file__).parent.parent / "config.example.json")


class StubLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def complete(self, system, user, max_tokens=500):
        if self.error:
            raise self.error
        return self.reply


class ParsingTests(unittest.TestCase):
    def test_slack_markup_cleaned(self):
        t = clean_text("<@U123> see <https://www.acme.example/x|Acme site> or <mailto:a@b.example|a@b.example>")
        self.assertEqual(t, "see Acme site https://www.acme.example/x or a@b.example")

    def test_domains_skip_free_mail_and_files(self):
        d = find_domains("mail dana@gmail.com, cc lee@northwind-analytics.example, deck.pdf", CFG["ignore_domains"])
        self.assertEqual(d, ["northwind-analytics.example"])

    def test_candidates_skip_stopwords_and_quarters(self):
        self.assertEqual(name_candidates("Acme is evaluating vendors, budget in Q3")[:1], ["Acme"])
        self.assertNotIn("Q3", name_candidates("Budget in Q3 for Initech"))

    def test_leading_name_colon_goes_first(self):
        self.assertEqual(name_candidates("Acme Robotics: they want a demo with Priya")[0], "Acme Robotics")

    def test_summary_is_first_sentence(self):
        self.assertEqual(summarize("Budget set for Q3. More detail follows."), "Budget set for Q3.")
        self.assertTrue(summarize("x" * 300).endswith("..."))


class PipelineTests(unittest.TestCase):
    def test_fixture_messages(self):
        expected = ["match", "match", "ambiguous", "match", "none", "none"]
        msgs = json.loads((FIX / "messages.json").read_text())
        got = [analyze(m["text"], CFG, SOURCE, summoned=m.get("mention", False)).status for m in msgs]
        self.assertEqual(got, expected)

    def test_intel_type_keywords(self):
        d = analyze("Acme is evaluating vendors, budget in Q3", CFG, SOURCE)
        self.assertEqual(d.extraction.intel_type, "buying_signal")
        self.assertEqual(d.method, "prefix")

    def test_email_domain_match(self):
        d = analyze("Spoke with dana@northwind-analytics.example about onboarding emails", CFG, SOURCE)
        self.assertEqual((d.status, d.method, d.resolution.account["Id"]), ("match", "domain", "001FAKE0002"))

    def test_llm_result_used(self):
        reply = json.dumps({"is_intel": True, "company_name": "Initech", "company_domain": None, "contact_name": "Priya Shah",
                            "intel_type": "relationship", "summary": "Met marketing director at dinner"})
        d = analyze("they said hi", CFG, SOURCE, StubLLM(reply))
        self.assertEqual((d.status, d.extraction.source, d.extraction.contact_name), ("match", "llm", "Priya Shah"))

    def test_llm_not_intel_is_dropped_unless_summoned(self):
        reply = json.dumps({"is_intel": False, "company_name": None, "company_domain": None, "contact_name": None,
                            "intel_type": "other", "summary": ""})
        self.assertEqual(analyze("Who takes the Acme call?", CFG, SOURCE, StubLLM(reply)).status, "not_intel")

    def test_llm_error_falls_back_to_keywords(self):
        d = analyze("Acme is evaluating vendors", CFG, SOURCE, StubLLM(error=LLMError("down")))
        self.assertEqual((d.status, d.extraction.source), ("match", "heuristic-fallback"))

    def test_llm_garbage_falls_back_to_keywords(self):
        d = analyze("Acme is evaluating vendors", CFG, SOURCE, StubLLM("sorry, I cannot do that"))
        self.assertEqual((d.status, d.extraction.source), ("match", "heuristic-fallback"))
        self.assertEqual(extract("Acme budget", CFG, StubLLM('{"company_name": 5, "intel_type": "x"}')).source in ("llm", "heuristic-fallback"), True)


if __name__ == "__main__":
    unittest.main()
