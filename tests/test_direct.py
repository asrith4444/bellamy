"""Direct (no-AI) form filling: option matching, confirmation text and the worker commands."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from bellamy import direct, worker


class OptionTests(unittest.TestCase):
    def test_best_option_prefers_exact_then_loose_then_prefix(self):
        options = ["United States Minor Outlying Islands+1", "United States+1", "Canada+1"]
        self.assertEqual(direct.best_option("United States +1", options), 1)
        self.assertEqual(direct.best_option("united states+1", options), 1)
        self.assertEqual(direct.best_option("No", ["Not sure", "No", "Yes"]), 1)
        self.assertEqual(direct.best_option("United States", ["Canada", "United States of America"]), 1)
        self.assertIsNone(direct.best_option("Mars", ["Canada", "Mexico"]))
        self.assertIsNone(direct.best_option("Yes", []))

    def test_search_boxes_take_the_first_result_for_the_city(self):
        results = ["Austin, Texas, United States", "Austintown, Ohio, United States"]
        self.assertEqual(direct.best_option("Austin, TX", results, search=True), 0)
        self.assertIsNone(direct.best_option("Austin, TX", results))
        schools = ["University of Colorado Denver", "University of Colorado at Boulder"]
        self.assertEqual(direct.best_option("University of Colorado Boulder", schools, search=True), 1)

    def test_search_text_drops_dial_codes(self):
        self.assertEqual(direct.search_text("United States +1"), "United States")
        self.assertEqual(direct.search_text("No"), "No")

    def test_confirmation_words(self):
        self.assertTrue(direct.CONFIRMATION.search("Thank you for applying to GitLab!"))
        self.assertTrue(direct.CONFIRMATION.search("Your application has been submitted."))
        self.assertFalse(direct.CONFIRMATION.search("Submit application"))


class WorkerDirectTests(unittest.TestCase):
    def make(self):
        instance = worker.Worker()
        instance._ensure = mock.AsyncMock()
        instance.browser = mock.MagicMock()
        instance.browser.navigate_to = mock.AsyncMock()
        instance.browser.start = mock.AsyncMock()
        instance.browser.must_get_current_page = mock.AsyncMock(return_value="page")
        return instance

    def test_fill_direct_opens_the_url_and_reports_without_ai_cost(self):
        instance = self.make()
        report = {"fields": [{"label": "First Name", "value": "Jordan", "status": "filled", "note": ""}],
                  "resume_uploaded": True, "unfilled_required": [], "blockers": [], "ready_to_submit": True}
        with mock.patch.object(direct, "wait_for", mock.AsyncMock(return_value=True)), \
                mock.patch.object(direct, "settle", mock.AsyncMock()), \
                mock.patch.object(direct, "fill", mock.AsyncMock(return_value=report)) as fill:
            reply = asyncio.run(instance.handle({"cmd": "fill_direct", "url": "https://x/1", "steps": [{"selector": "#a"}]}))
        instance.browser.navigate_to.assert_awaited_once_with("https://x/1")
        fill.assert_awaited_once_with("page", [{"selector": "#a"}])
        self.assertTrue(reply["result"]["ready_to_submit"] and reply["success"])
        self.assertEqual(reply["usage"]["cost"], 0.0)

    def test_fill_direct_reports_a_form_that_never_loads(self):
        instance = self.make()
        with mock.patch.object(direct, "wait_for", mock.AsyncMock(return_value=False)):
            reply = asyncio.run(instance.handle({"cmd": "fill_direct", "url": "https://x/1", "steps": [{"selector": "#a"}]}))
        self.assertEqual(reply["result"]["blockers"], ["error: the form did not load"])

    def test_submit_direct(self):
        instance = self.make()
        done = {"submitted": True, "confirmation_text": "Thank you for applying", "blockers": []}
        with mock.patch.object(direct, "submit", mock.AsyncMock(return_value=done)) as submit:
            reply = asyncio.run(instance.handle({"cmd": "submit_direct"}))
        submit.assert_awaited_once_with("page", "button[type=submit]", wait=30.0)
        self.assertTrue(reply["result"]["submitted"])


if __name__ == "__main__":
    unittest.main()
