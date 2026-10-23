"""Application forms: structured results for read / fill / submit, and the submit guard.

The guard is a deterministic backstop for fill mode: whatever the model decides,
clicks on a final submit button, Enter key presses and JS form submits are blocked
until the caller explicitly asks to submit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


class FormField(BaseModel):
    label: str
    kind: Literal["text", "email", "phone", "textarea", "select", "radio", "checkbox", "file",
                  "date", "number", "url", "other"] = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)


class FormRead(BaseModel):
    is_application_form: bool = False
    company: str = ""
    job_title: str = ""
    fields: list[FormField] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list, description="captcha | login_required | job_closed | error")


class FilledField(BaseModel):
    label: str = ""
    value: str = ""
    status: Literal["filled", "skipped", "failed"] = "filled"
    note: str = ""


class FillReport(BaseModel):
    fields: list[FilledField] = Field(default_factory=list)
    resume_uploaded: bool = False
    unfilled_required: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    ready_to_submit: bool = False


class SubmitResult(BaseModel):
    submitted: bool = False
    confirmation_text: str = ""
    blockers: list[str] = Field(default_factory=list)
    url: str = ""  # where the page was after the submit
    page_text: str = ""  # what it said, so an unconfirmed submit can be judged
    # The site emailed a code and holds the application until it is entered (enter_code).
    code_required: bool = False
    code_prompt: str = ""  # what the page says about the code, e.g. who it was sent to
    code_length: int = 0  # characters the code has, when the page says (Greenhouse: 8)
    code_error: str = ""  # why the last code entered was turned down
    # Submit drew no reaction at all, even pressed twice: the form was never sent and is still open.
    form_unchanged: bool = False


_FINAL = re.compile(r"\b(submit|send (?:my |your )?application|finish application|complete application)\b", re.I)
_APPLY = re.compile(r"^\s*apply(?: now)?\s*$", re.I)


def node_text(node: Any) -> str:
    parts = []
    for getter in ("get_all_children_text", "get_meaningful_text_for_llm"):
        fn = getattr(node, getter, None)
        if callable(fn):
            try:
                parts.append(str(fn() or ""))
            except Exception:
                pass
            break
    attrs = getattr(node, "attributes", None) or {}
    parts += [str(attrs.get(k, "")) for k in ("value", "aria-label", "title")]
    return " ".join(p for p in parts if p).strip()


def is_final_submit(node: Any) -> bool:
    """A button that would send the application (not "Apply" links that open the form, not "Next")."""
    if node is None:
        return False
    attrs = getattr(node, "attributes", None) or {}
    text = node_text(node)
    if _FINAL.search(text):
        return True
    is_submit_type = str(attrs.get("type", "")).lower() == "submit"
    return is_submit_type and (not text or bool(_APPLY.match(text)))


def _actions(model_output: Any) -> list[dict]:
    result = []
    for action in getattr(model_output, "action", []) or []:
        if hasattr(action, "model_dump"):
            result.append(action.model_dump(exclude_none=True))
        elif isinstance(action, dict):
            result.append(action)
    return result


@dataclass
class SubmitGuard:
    active: bool = True
    blocked: int = 0

    def violation(self, browser_state: Any, model_output: Any) -> str | None:
        if not self.active:
            return None
        try:
            selector_map = browser_state.dom_state.selector_map
        except AttributeError:
            selector_map = {}
        for action in _actions(model_output):
            if "click" in action:
                index = (action["click"] or {}).get("index")
                if is_final_submit(selector_map.get(index)):
                    return f"click on submit button #{index}"
            if "send_keys" in action and "enter" in str(action["send_keys"]).lower():
                return "Enter key press (could submit the form)"
            if "evaluate" in action and "submit" in str(action["evaluate"]).lower():
                return "script that submits the form"
        return None

    def review(self, browser_state: Any, model_output: Any, action_model: Any) -> str | None:
        """Replace a submitting action. First time: a no-op plus a note; second time: finish."""
        reason = self.violation(browser_state, model_output)
        if reason is None:
            return None
        self.blocked += 1
        message = f"Submit guard: blocked {reason}. This run fills the form only; never submit."
        if self.blocked >= 2:
            payload = {"done": {"data": {"blockers": ["tried to submit in fill mode"]}}}
        else:
            payload = {"wait": {"seconds": 1}}
            model_output.memory = ((getattr(model_output, "memory", "") or "") + "\n" + message).strip()
            model_output.next_goal = "Finish filling, then call done with the report. Do not submit."
        try:
            model_output.action = [action_model.model_validate(payload)]
        except Exception:
            model_output.action = [action_model.model_validate({"wait": {"seconds": 1}})]
        return message
