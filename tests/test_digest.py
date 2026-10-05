import json
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from slack_account_intel.config import load_config
from slack_account_intel.digest import build_digest_prompt, fallback_digest, make_digest, truncate
from slack_account_intel.llm import AnthropicClient, LLMError

TASKS = json.loads((Path(__file__).parent.parent / "fixtures" / "intel_tasks.json").read_text())


class FakeLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def complete(self, system, user, max_tokens=600):
        self.calls.append((system, user, max_tokens))
        if self.error:
            raise self.error
        return self.reply


class PromptTests(unittest.TestCase):
    def test_prompt_contents_and_order(self):
        system, user = build_digest_prompt("Acme Robotics", TASKS, "- old fact", 800, 10)
        self.assertIn("Account: Acme Robotics", user)
        self.assertIn("Character limit: 800", user)
        self.assertIn("- old fact", user)
        # newest first
        self.assertLess(user.index("2025-11-20"), user.index("2025-11-02"))
        self.assertLess(user.index("2025-11-02"), user.index("2025-10-11"))
        # bot footer and Slack link are not sent to the model
        self.assertNotIn("Logged via", user)
        self.assertNotIn("example.slack.com", user)
        self.assertIn("never add facts", system)

    def test_prompt_respects_max_items(self):
        _, user = build_digest_prompt("Acme Robotics", TASKS, "", 800, 1)
        self.assertIn("2025-11-20", user)
        self.assertNotIn("2025-10-11", user)

    def test_empty_previous(self):
        self.assertIn("(empty)", build_digest_prompt("A", TASKS, "", 800, 5)[1])


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_llm_result_used_and_truncated(self):
        llm = FakeLLM(reply="- " + "word " * 500)
        d = make_digest(llm, "Acme Robotics", TASKS, "", self.cfg)
        self.assertEqual(d.source, "llm")
        self.assertLessEqual(len(d.text), self.cfg["digest"]["max_chars"])
        self.assertEqual(llm.calls[0][2], self.cfg["llm"]["digest_max_tokens"])

    def test_fallback_when_llm_errors(self):
        d = make_digest(FakeLLM(error=LLMError("boom")), "Acme Robotics", TASKS, "", self.cfg)
        self.assertEqual(d.source, "fallback")
        self.assertTrue(d.text.startswith("- 2025-11-20: Ops lead wants a security review"))

    def test_fallback_when_llm_returns_blank(self):
        self.assertEqual(make_digest(FakeLLM(reply="  "), "A", TASKS, "", self.cfg).source, "fallback")

    def test_fallback_without_llm(self):
        self.assertEqual(make_digest(None, "A", TASKS, "", self.cfg).source, "fallback")

    def test_fallback_fits_limit_on_whole_lines(self):
        text = fallback_digest(TASKS, 100, 10)
        self.assertLessEqual(len(text), 100)
        self.assertTrue(all(line.startswith("- ") for line in text.splitlines()))

    def test_no_tasks_keeps_previous(self):
        self.assertEqual(make_digest(FakeLLM(reply="x"), "A", [], "- keep me", self.cfg).text, "- keep me")

    def test_truncate_on_word_boundary(self):
        self.assertEqual(truncate("alpha beta gamma", 12), "alpha...")


class AnthropicClientTests(unittest.TestCase):
    def test_request_shape(self):
        c = AnthropicClient("test-key", "claude-sonnet-5-5")
        req = c.build_request("sys", "hello", 100)
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), "test-key")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["system"], body["max_tokens"]), ("claude-sonnet-5-5", "sys", 100))

    def test_network_failure_becomes_llmerror(self):
        c = AnthropicClient("k", "m")
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(LLMError):
                c.complete("s", "u")


if __name__ == "__main__":
    unittest.main()
