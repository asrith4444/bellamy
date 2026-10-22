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


if __name__ == "__main__":
    unittest.main()
