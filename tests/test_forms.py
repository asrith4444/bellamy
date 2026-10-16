"""Deterministic tests for the fill-mode submit guard and the worker protocol."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from bellamy.forms import FillReport, SubmitGuard, is_final_submit
from bellamy.worker import serve


class Node:
    def __init__(self, text="", **attributes):
        self.text, self.attributes = text, attributes

    def get_all_children_text(self):
        return self.text


class FakeAction:
    def __init__(self, data):
        self.data = data

    def model_dump(self, exclude_none=True):
        return self.data


class FakeActionModel:
    @staticmethod
    def model_validate(payload):
        return FakeAction(payload)


def state(**nodes):
    return SimpleNamespace(dom_state=SimpleNamespace(selector_map={int(k[1:]): v for k, v in nodes.items()}))


def output(*actions):
    return SimpleNamespace(action=[FakeAction(a) for a in actions], memory="", next_goal="")


class SubmitDetectionTests(unittest.TestCase):
    def test_final_submit_buttons(self):
        self.assertTrue(is_final_submit(Node("Submit application", type="submit")))
        self.assertTrue(is_final_submit(Node("Submit")))
        self.assertTrue(is_final_submit(Node("", type="submit")))
        self.assertTrue(is_final_submit(Node("Apply", type="submit")))

    def test_navigation_buttons_are_allowed(self):
        self.assertFalse(is_final_submit(Node("Apply for this job")))
        self.assertFalse(is_final_submit(Node("Apply")))
        self.assertFalse(is_final_submit(Node("Next", type="submit")))
        self.assertFalse(is_final_submit(Node("Upload resume")))
        self.assertFalse(is_final_submit(None))


class SubmitGuardTests(unittest.TestCase):
    def test_blocks_then_finishes(self):
        guard = SubmitGuard(active=True)
        s = state(n7=Node("Submit application", type="submit"))
        first = output({"click": {"index": 7}})
        self.assertIn("blocked", guard.review(s, first, FakeActionModel))
        self.assertEqual(first.action[0].data, {"wait": {"seconds": 1}})
        self.assertIn("never submit", first.memory)
        second = output({"click": {"index": 7}})
        guard.review(s, second, FakeActionModel)
        self.assertEqual(second.action[0].data, {"done": {"data": {"blockers": ["tried to submit in fill mode"]}}})

    def test_blocks_enter_and_js_submit(self):
        guard = SubmitGuard(active=True)
        self.assertIsNotNone(guard.violation(state(), output({"send_keys": {"keys": "Enter"}})))
        self.assertIsNotNone(guard.violation(state(), output({"evaluate": {"code": "document.forms[0].submit()"}})))

    def test_allows_filling_and_inactive_guard(self):
        guard = SubmitGuard(active=True)
        s = state(n3=Node("First name"), n7=Node("Submit"))
        self.assertIsNone(guard.review(s, output({"input": {"index": 3, "text": "Jordan"}}), FakeActionModel))
        self.assertIsNone(SubmitGuard(active=False).review(s, output({"click": {"index": 7}}), FakeActionModel))


class FakeWorker:
    def __init__(self):
        self.closed = False

    async def handle(self, command):
        if command["cmd"] == "boom":
            raise RuntimeError("browser crashed")
        return {"result": FillReport(ready_to_submit=True).model_dump(), "usage": {"cost": 0.01}, "success": True}

    async def close(self):
        self.closed = True


class WorkerProtocolTests(unittest.TestCase):
    def test_serve_replies_per_line_and_survives_errors(self):
        lines = iter([json.dumps({"id": 1, "cmd": "fill"}) + "\n", "not json\n",
                      json.dumps({"id": 2, "cmd": "boom"}) + "\n", json.dumps({"id": 3, "cmd": "close"}) + "\n"])
        out, worker = [], FakeWorker()

        async def reader():
            return next(lines, "")

        asyncio.run(serve(worker, reader, out.append))
        self.assertEqual([o["id"] for o in out], [1, None, 2, 3])
        self.assertTrue(out[0]["ok"] and out[0]["result"]["ready_to_submit"])
        self.assertFalse(out[1]["ok"])
        self.assertEqual(out[2]["error"], "RuntimeError: browser crashed")
        self.assertTrue(worker.closed)


if __name__ == "__main__":
    unittest.main()
