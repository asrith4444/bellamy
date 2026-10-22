"""The worker's model settings: brief reasoning and a longer per-call limit (no network)."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import httpx

from bellamy import worker
from bellamy.forms import FormRead


def completion(model: str) -> dict:
    return {
        "id": "test", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def sent_request(model: str) -> dict:
    """The JSON body the worker's model client sends for one call."""
    from browser_use.llm.messages import UserMessage

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=completion(model))

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            llm = worker.make_llm(model, api_key="test-key", base_url="https://llm.test/v1", http_client=client)
            await llm.ainvoke([UserMessage(content="hello")])

    asyncio.run(call())
    return seen


class ReasoningEffortTests(unittest.TestCase):
    def test_glm_is_asked_for_low_reasoning(self):
        body = sent_request("z-ai/glm-5.3-flash")

        self.assertEqual(body["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(body["reasoning_effort"], "low")

    def test_empty_setting_leaves_the_model_default(self):
        with mock.patch.object(worker, "REASONING_EFFORT", ""):
            body = sent_request("z-ai/glm-5.3-flash")

        self.assertNotIn("reasoning_effort", body)


class StepLimitTests(unittest.TestCase):
    def test_agent_gets_the_longer_per_call_limit(self):
        import browser_use

        captured: dict = {}

        class FakeHistory:
            structured_output = None
            usage = None

            def is_successful(self):
                return False

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def run(self, max_steps):
                return FakeHistory()

        form_worker = worker.Worker(model="z-ai/glm-5.3-flash")
        form_worker.browser, form_worker.llm = object(), object()  # skip launching a real browser
        with mock.patch.object(browser_use, "Agent", FakeAgent):
            asyncio.run(form_worker.run("read the form", FormRead, max_steps=1, guard_active=True))

        self.assertEqual(captured["llm_timeout"], worker.LLM_TIMEOUT)
        self.assertGreater(worker.LLM_TIMEOUT, 75, "browser-use's default limit was too short for big forms")



class RepairProtocolTests(unittest.TestCase):
    def test_read_current_form_preserves_session(self):
        instance = worker.Worker()
        instance.run = mock.AsyncMock(return_value=(FormRead(), {}, True))
        asyncio.run(instance.handle({"cmd": "read_form", "url": None}))
        task = instance.run.call_args.args[0]
        self.assertIn("do not navigate away", task)
        self.assertIn("phone-country", task)
        self.assertTrue(instance.run.call_args.kwargs["guard_active"])

    def test_repair_pass_includes_failures_and_keeps_submit_guard(self):
        from bellamy.forms import FillReport
        instance = worker.Worker()
        instance.run = mock.AsyncMock(return_value=(FillReport(), {}, True))
        asyncio.run(instance.handle({"cmd": "fill", "plan": [], "repair": ["resume upload incomplete"], "max_steps": 20}))
        self.assertIn("resume upload incomplete", instance.run.call_args.args[0])
        self.assertTrue(instance.run.call_args.kwargs["guard_active"])
        self.assertEqual(instance.run.call_args.kwargs["max_steps"], 20)


class AutoOpenTests(unittest.TestCase):
    def test_agent_never_opens_a_url_found_in_the_task(self):
        from bellamy.forms import FillReport

        instance = worker.Worker()
        instance._ensure = mock.AsyncMock()
        instance.llm, instance.browser = object(), object()
        history = mock.MagicMock(structured_output=FillReport(), usage=None)
        history.is_successful.return_value = True
        agent = mock.MagicMock()
        agent.run = mock.AsyncMock(return_value=history)
        with mock.patch("browser_use.Agent", return_value=agent) as make_agent:
            task = worker.fill_task({"plan": [{"label": "LinkedIn Profile", "value": "linkedin.com/in/jordan-example"}]})
            asyncio.run(instance.run(task, FillReport, max_steps=5, guard_active=True))
        self.assertIs(make_agent.call_args.kwargs["directly_open_url"], False)


if __name__ == "__main__":
    unittest.main()
