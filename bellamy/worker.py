"""Bellamy worker: a JSON-lines session that keeps one browser open across commands,
so a caller (Mark) can read a form, fill it, check the result and only then submit.

Run:  python -m bellamy.worker [--headed]
Send one JSON object per line on stdin; get one JSON object per line on stdout:

  {"id": 1, "cmd": "read_form", "url": "..."}                       -> FormRead
  {"id": 2, "cmd": "fill", "plan": [{"label", "value", "kind"}], "files": {"resume": "/path.pdf"}}
                                                                    -> FillReport (never submits)
  Without "url", read_form and fill work on the page already open (to re-read or repair a
  filled form); fill's optional "repair" lists the problems a check found in the last fill.
  {"id": 3, "cmd": "submit"}                                        -> SubmitResult
  {"id": 4, "cmd": "screenshot", "path": "/tmp/x.png"}              -> {"path": ...}
  {"id": 5, "cmd": "close"}

Each reply: {"id", "ok", "result", "usage": {"cost", "prompt_tokens", "completion_tokens", "model"},
"success", "error"}. Logs go to stderr; stdout carries only protocol lines.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

from .paths import ENV_PATH

load_dotenv(ENV_PATH)

from pydantic import BaseModel  # noqa: E402

from .forms import FillReport, FormRead, SubmitGuard, SubmitResult  # noqa: E402

# Application forms are big pages. A model that thinks at length on every step outlasted
# browser-use's 75-second limit per model call, so ask for brief reasoning and allow a bit more time.
# BELLAMY_REASONING_EFFORT="" sends no effort setting, leaving the model's own default.
REASONING_EFFORT = os.getenv("BELLAMY_REASONING_EFFORT", "low")
LLM_TIMEOUT = int(os.getenv("BELLAMY_LLM_TIMEOUT", "120"))


def make_llm(model: str, **overrides: Any):
    """The worker's model client. browser-use sends a reasoning effort only for OpenAI's reasoning
    models; listing the configured model sends it for any model (OpenRouter applies it to GLM)."""
    from browser_use.llm.openai.chat import ChatOpenAI

    from .agent import get_api_key

    params: dict[str, Any] = {
        "model": model,
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "max_completion_tokens": 8192,
    }
    if REASONING_EFFORT:
        params.update(reasoning_models=[model], reasoning_effort=REASONING_EFFORT)
    params.update(overrides)
    if "api_key" not in params:
        params["api_key"] = get_api_key()
    return ChatOpenAI(**params)


FORM_PROTOCOL = """
=== APPLICATION FORM PROTOCOL ===
- Work only on the application form for the job you were given. Never create an account or log
  in unless the task says so; report login walls as a blocker instead.
- Use exactly the values you are given. Never invent personal data.
- Upload files only from the exact paths you are given.
- If you see a CAPTCHA, stop and report "captcha" in blockers.
- Finish with done and the structured result.
"""

READ_FORM_TASK = """{where}If it shows a job description with an Apply button, click it to open the
application form (do not log in or create an account). Then list every field in the application form from
top to bottom: text inputs, text areas, file uploads, dropdowns, radio groups and checkboxes. For each field
give its exact visible label, its kind, whether it is required (an asterisk or "required"), and for choice
fields the visible options. Scroll to the end so nothing is missed. Do not type anything and do not submit.
Report "captcha", "login_required" or "job_closed" in blockers when you see them."""

FILL_TASK = """{where}Fill the application form with exactly these values (label => value):
{plan}
{files}For dropdowns, radio groups and checkboxes pick the option that matches the value. Leave any field that
is not in the list empty; if it is required, list its label in unfilled_required. Do NOT click the final
submit button: stop once every listed field is filled. Then scroll through the whole form once and report,
for each field, the value it now shows. Set ready_to_submit to true only when every required field is filled
and no error messages are shown."""

REPAIR_NOTE = """A check of the last fill found these problems. Fix each one; keep every field that is
already correct as it is:
{problems}
"""
CURRENT_PAGE = "Stay on the page that is already open: do not navigate away or reload, since the form may be partly filled.\n"


def read_form_task(url: str | None) -> str:
    return READ_FORM_TASK.format(where=f"Open {url}. " if url else CURRENT_PAGE)


def fill_task(command: dict) -> str:
    plan = "\n".join(f"- {item['label']} => {item['value']}" for item in command.get("plan", []))
    files = command.get("files") or {}
    files_text = "".join(f"Upload the {name} file from this exact path: {path}\n" for name, path in files.items())
    where = f"Open {command['url']}.\n" if command.get("url") else CURRENT_PAGE
    repair = [str(p) for p in command.get("repair") or [] if str(p).strip()]
    if repair:
        where += REPAIR_NOTE.format(problems="\n".join(f"- {p}" for p in repair))
    return FILL_TASK.format(where=where, plan=plan, files=files_text)


SUBMIT_TASK = """The application form on the current page is complete. Click its final submit button exactly
once and wait for the page to change. Set submitted to true only if you see a confirmation such as "Thank you
for applying" or "Application submitted", and copy that confirmation text. If a CAPTCHA or validation errors
appear, do not retry: report them in blockers."""


def _usage(history: Any, model: str) -> dict:
    usage = getattr(history, "usage", None)
    if usage is None:
        return {"cost": None, "prompt_tokens": 0, "completion_tokens": 0, "model": model}
    return {
        "cost": getattr(usage, "total_cost", None),
        "prompt_tokens": getattr(usage, "total_prompt_tokens", 0),
        "completion_tokens": getattr(usage, "total_completion_tokens", 0),
        "model": model,
    }


class Worker:
    def __init__(self, headless: bool = True, model: str | None = None):
        self.headless = headless
        self.model = model or os.getenv("DEFAULT_MODEL", "openai/gpt-5.6-luna")
        self.browser = None
        self.llm = None

    async def _ensure(self) -> None:
        if self.browser is not None:
            return
        from browser_use import Browser

        from .stealth import build_stealth_profile

        self.browser = Browser(browser_profile=build_stealth_profile(headless=self.headless, keep_alive=True))
        self.llm = make_llm(self.model)

    async def run(self, task: str, output_model: type[BaseModel], *, max_steps: int, guard_active: bool,
                  files: list[str] | None = None) -> tuple[BaseModel | None, dict, bool]:
        """One browser-use agent run on the shared browser. Returns (structured result, usage, success)."""
        await self._ensure()
        from browser_use import Agent
        from browser_use.tools.service import Tools

        from .agent import BELLAMY_PERSONA
        from .jobs import STEALTH_BEHAVIOR_PROMPT
        from .stealth import human_step_callback
        from .step_guard import StepCircuitBreaker

        guard = SubmitGuard(active=guard_active)
        breaker = StepCircuitBreaker()
        holder: dict = {}

        async def on_step(state, output, step):
            agent = holder.get("agent")
            message = None
            if agent is not None:
                message = guard.review(state, output, agent.ActionModel) or breaker.review(state, output, agent.ActionModel)
            if message:
                print(f"[bellamy-worker] {message}", file=sys.stderr)
            else:
                await human_step_callback(state, output, step)

        agent = Agent(
            task=task, llm=self.llm, browser=self.browser, tools=Tools(output_model=output_model),
            output_model_schema=output_model, available_file_paths=files or [],
            calculate_cost=True, use_judge=False, use_vision="auto", llm_screenshot_size=(1024, 640), llm_timeout=LLM_TIMEOUT,
            max_history_items=8, max_actions_per_step=5, message_compaction=True,
            extend_system_message=BELLAMY_PERSONA + "\n" + STEALTH_BEHAVIOR_PROMPT + "\n" + FORM_PROTOCOL,
            register_new_step_callback=on_step, save_conversation_path=None,
        )
        holder["agent"] = agent
        history = await agent.run(max_steps=max_steps)
        structured = None
        try:
            structured = history.structured_output
        except Exception:
            structured = None
        return structured, _usage(history, self.model), bool(history.is_successful())

    async def handle(self, command: dict) -> dict:
        cmd = command.get("cmd")
        if cmd == "read_form":
            result, usage, success = await self.run(read_form_task(command.get("url")), FormRead,
                                                    max_steps=command.get("max_steps", 20), guard_active=True)
            return {"result": (result or FormRead(blockers=["no result"])).model_dump(), "usage": usage, "success": success}
        if cmd == "fill":
            files = command.get("files") or {}
            result, usage, success = await self.run(fill_task(command), FillReport, max_steps=command.get("max_steps", 60),
                                                    guard_active=True, files=list(files.values()))
            return {"result": (result or FillReport(blockers=["no result"])).model_dump(), "usage": usage, "success": success}
        if cmd == "submit":
            result, usage, success = await self.run(SUBMIT_TASK, SubmitResult, max_steps=command.get("max_steps", 10),
                                                    guard_active=False)
            return {"result": (result or SubmitResult(blockers=["no result"])).model_dump(), "usage": usage, "success": success}
        if cmd == "screenshot":
            await self._ensure()
            await self.browser.take_screenshot(path=command["path"], full_page=True)
            return {"result": {"path": command["path"]}}
        raise ValueError(f"unknown command {cmd!r}")

    async def close(self) -> None:
        if self.browser is not None:
            try:
                await self.browser.kill()
            except Exception:
                pass
            self.browser = None


async def serve(worker: Worker, reader, write) -> None:
    """Read commands until EOF or close; write one reply per command."""
    while True:
        line = await reader()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            write({"id": None, "ok": False, "error": f"bad JSON: {exc}"})
            continue
        if command.get("cmd") == "close":
            write({"id": command.get("id"), "ok": True, "result": {}})
            break
        try:
            reply = await worker.handle(command)
            write({"id": command.get("id"), "ok": True, **reply})
        except Exception as exc:  # one failed command must not kill the session
            write({"id": command.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="bellamy-worker")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("-m", "--model", default=None)
    args = parser.parse_args()

    # Keep stdout for protocol lines only: anything else printed goes to stderr.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def write(obj: dict) -> None:
        protocol.write(json.dumps(obj, default=str) + "\n")
        protocol.flush()

    loop_reader = lambda: asyncio.to_thread(sys.stdin.readline)  # noqa: E731
    asyncio.run(serve(Worker(headless=not args.headed, model=args.model), loop_reader, write))


if __name__ == "__main__":
    main()
