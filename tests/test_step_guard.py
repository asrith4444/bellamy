"""Tests for Bellamy's repeated-dead-end circuit breaker."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from bellamy.step_guard import StepCircuitBreaker, detect_hard_error


class FakeDom:
    def __init__(self, text: str):
        self.text = text

    def llm_representation(self):
        return self.text


class FakeAction:
    def __init__(self, payload: dict):
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


class FakeActionModel:
    @classmethod
    def model_validate(cls, payload):
        return FakeAction(payload)


def output(payload: dict, goal: str):
    return SimpleNamespace(
        action=[FakeAction(payload)],
        thinking="",
        evaluation_previous_goal="Failure",
        memory="",
        next_goal=goal,
    )


class StepCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(
            url="https://www.dice.com/jobs?q=ai",
            dom_state=FakeDom("Something went wrong. Try Again. Reload Page."),
        )
        self.guard = StepCircuitBreaker()

    def test_detects_hard_error_text(self):
        self.assertEqual(detect_hard_error("DNS_PROBE_FINISHED_NXDOMAIN"), "dns")
        self.assertEqual(detect_hard_error("normal job results"), None)

    def test_allows_one_retry_then_forces_fresh_decision(self):
        first = output({"click": {"index": 4}}, "Click Try Again once")
        self.assertIsNone(self.guard.review(self.state, first, FakeActionModel))

        second = output({"click": {"index": 4}}, "Reload current page")
        message = self.guard.review(self.state, second, FakeActionModel)

        self.assertIn("Circuit breaker", message)
        self.assertEqual(second.action[0].payload, {"wait": {"seconds": 1}})
        self.assertIn("genuinely different", second.next_goal)

    def test_second_blocked_retry_finishes_unsuccessfully(self):
        self.guard.review(self.state, output({"click": {"index": 4}}, "Try Again"), FakeActionModel)
        self.guard.review(self.state, output({"click": {"index": 4}}, "Retry current page"), FakeActionModel)
        third = output({"click": {"index": 4}}, "Reload the same URL")

        self.guard.review(self.state, third, FakeActionModel)

        self.assertFalse(third.action[0].payload["done"]["success"])
        self.assertIn("Stopped", third.action[0].payload["done"]["text"])

    def test_genuine_endpoint_pivot_remains_allowed(self):
        self.guard.review(self.state, output({"click": {"index": 4}}, "Try Again"), FakeActionModel)
        self.guard.review(self.state, output({"click": {"index": 4}}, "Retry current page"), FakeActionModel)
        pivot = output(
            {"navigate": {"url": "https://duckduckgo.com/?q=site%3Adice.com+AI"}},
            "Use a different site",
        )

        self.assertIsNone(self.guard.review(self.state, pivot, FakeActionModel))
        self.assertIn("navigate", pivot.action[0].payload)

    def test_rate_limit_stops_immediately(self):
        state = SimpleNamespace(
            url="https://example.com/search",
            dom_state=FakeDom("Too many requests. Please slow down."),
        )
        proposed = output({"wait": {"seconds": 5}}, "Wait and retry")

        self.guard.review(state, proposed, FakeActionModel)

        self.assertFalse(proposed.action[0].payload["done"]["success"])


if __name__ == "__main__":
    unittest.main()
