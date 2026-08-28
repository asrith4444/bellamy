"""Deterministic tests for Bellamy's job-search planner."""

from __future__ import annotations

import unittest

from bellamy.jobs import build_job_search_task, split_boolean_query


class JobSearchPlannerTests(unittest.TestCase):
    def test_splits_boolean_or_into_bounded_phrases(self):
        self.assertEqual(
            split_boolean_query("entry level AI developer OR full stack developer"),
            ["entry level AI developer", "full stack developer"],
        )

    def test_dice_uses_independent_current_web_urls(self):
        task = build_job_search_task(
            "entry level AI developer OR full stack developer",
            sites="dice",
        )

        self.assertIn("q=entry+level+AI+developer", task)
        self.assertIn("q=full+stack+developer", task)
        self.assertNotIn("q=entry+level+AI+developer+OR+full+stack+developer", task)
        self.assertIn("Never use service.dice.com", task)
        self.assertIn("Do not guess API hosts", task)
        self.assertIn("one browser instance", task)

    def test_location_is_encoded_in_each_dice_url(self):
        task = build_job_search_task(
            "AI developer OR backend developer",
            location="New York, NY",
            sites="dice",
        )
        self.assertEqual(task.count("location=New+York%2C+NY"), 2)


if __name__ == "__main__":
    unittest.main()
