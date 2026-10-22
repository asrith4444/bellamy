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


# What browser-use showed the agent on a Greenhouse application form (trimmed).
GREENHOUSE_FORM = (
    "[2101]<input name=resume type=file />\nResume/CV*\n"
    "|scroll element|<iframe title=reCAPTCHA role=presentation name=a-f5px2sqivtg2 /> (scroll)\n"
    "\t[2116]<div />\n[2120]<button type=submit />\n\tSubmit application"
)


class CaptchaDetectionTests(unittest.TestCase):
    def test_invisible_recaptcha_on_an_application_form_is_not_a_hard_error(self):
        self.assertIsNone(detect_hard_error(GREENHOUSE_FORM))

    def test_captcha_notices_are_not_hard_errors(self):
        self.assertIsNone(detect_hard_error(
            "This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply."))
        self.assertIsNone(detect_hard_error("This form is protected by hCaptcha."))

    def test_visible_challenges_are_still_detected(self):
        for text in ("Verify you are human by completing the action below.",
                     "Please complete the CAPTCHA to continue.", "I'm not a robot"):
            with self.subTest(text=text):
                self.assertEqual(detect_hard_error(text), "captcha")

    def test_breaker_lets_an_application_form_through(self):
        state = SimpleNamespace(url="https://job-boards.greenhouse.io/acme/jobs/1", dom_state=FakeDom(GREENHOUSE_FORM))
        proposed = output({"scroll": {"down": True}}, "Scroll to the rest of the form")

        self.assertIsNone(StepCircuitBreaker().review(state, proposed, FakeActionModel))
        self.assertEqual(proposed.action[0].payload, {"scroll": {"down": True}})


def real_action_model(output_model=None):
    try:
        from browser_use.tools.service import Tools
    except ImportError as exc:
        raise unittest.SkipTest(f"browser-use not installed: {exc}")
    return Tools(output_model=output_model).registry.create_action_model()


class StopActionTests(unittest.TestCase):
    """The breaker's stop must be an action the run accepts, or browser-use fails the step."""

    def setUp(self):
        self.state = SimpleNamespace(url="https://jobs.example.com/apply", dom_state=FakeDom("Verify you are human"))

    def test_worker_run_stops_with_structured_blockers(self):
        from bellamy.forms import FormRead

        proposed = output({"scroll": {"down": True}}, "Scroll down")
        message = StepCircuitBreaker().review(self.state, proposed, real_action_model(FormRead))

        done = proposed.action[0].model_dump(exclude_none=True)["done"]
        self.assertIn("Circuit breaker", message)
        self.assertFalse(done["success"])
        self.assertEqual(done["data"]["blockers"], ["captcha on jobs.example.com/apply"])

    def test_free_form_run_stops_with_text(self):
        proposed = output({"scroll": {"down": True}}, "Scroll down")
        StepCircuitBreaker().review(self.state, proposed, real_action_model())

        done = proposed.action[0].model_dump(exclude_none=True)["done"]
        self.assertFalse(done["success"])
        self.assertIn("Stopped", done["text"])

    def test_waits_instead_of_raising_when_no_stop_fits(self):
        class WaitOnly:
            @staticmethod
            def model_validate(payload):
                if "wait" not in payload:
                    raise ValueError("unsupported action")
                return FakeAction(payload)

        proposed = output({"scroll": {"down": True}}, "Scroll down")
        StepCircuitBreaker().review(self.state, proposed, WaitOnly)

        self.assertEqual(proposed.action[0].payload, {"wait": {"seconds": 1}})
        self.assertIn("done(success=false)", proposed.next_goal)


if __name__ == "__main__":
    unittest.main()
