"""Runtime circuit breaker for repeated browser dead ends.

The browser-use model observes and reasons over every live DOM state. This guard
is deliberately narrower: it prevents the model from rationalizing repeated
reloads of the same hard-error endpoint after that reasoning has already failed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


HARD_ERROR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("captcha", ("captcha", "verify you are human", "are you a robot")),
    ("rate_limit", ("too many requests", "rate limit", "unusual activity")),
    ("dns", ("dns_probe_finished_nxdomain", "server ip address could not be found")),
    ("access_denied", ("access denied", "forbidden", "request blocked")),
    ("generic_site_error", ("something went wrong", "this site can’t be reached", "this site can't be reached")),
)

_RETRY_WORDS = ("retry", "try again", "reload", "refresh", "same url", "current page")


def detect_hard_error(dom_text: str) -> str | None:
    text = dom_text.lower()
    for name, markers in HARD_ERROR_MARKERS:
        if any(marker in text for marker in markers):
            return name
    return None


def endpoint_key(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.netloc.lower(), parsed.path.rstrip("/") or "/"


def _action_dicts(model_output: Any) -> list[dict]:
    result = []
    for action in getattr(model_output, "action", []) or []:
        if hasattr(action, "model_dump"):
            result.append(action.model_dump(exclude_none=True))
        elif isinstance(action, dict):
            result.append(action)
    return result


def _contains_done(actions: list[dict]) -> bool:
    return any("done" in action for action in actions)


def _navigation_urls(actions: list[dict]) -> list[str]:
    urls = []
    for action in actions:
        for name, params in action.items():
            if name in {"navigate", "go_to_url", "open_url"} and isinstance(params, dict) and params.get("url"):
                urls.append(str(params["url"]))
    return urls


def is_retry_decision(current_url: str, model_output: Any, *, endpoint_seen: int) -> bool:
    """Identify reload/retry behavior while allowing a genuine endpoint pivot."""
    actions = _action_dicts(model_output)
    if _contains_done(actions):
        return False

    goal = " ".join(
        str(getattr(model_output, field, "") or "")
        for field in ("thinking", "evaluation_previous_goal", "memory", "next_goal")
    ).lower()
    if any(word in goal for word in _RETRY_WORDS):
        return endpoint_seen >= 2

    current_endpoint = endpoint_key(current_url)
    nav_urls = _navigation_urls(actions)
    if any(endpoint_key(url) == current_endpoint for url in nav_urls):
        # A second query on the same route may be a legitimate first pivot. By
        # the third identical endpoint error, query variations are a dead end.
        return endpoint_seen >= 3

    # Clicking an error page after it has already failed once is usually its
    # Try Again/Reload control. Ordinary clicks on the first observation remain allowed.
    if endpoint_seen >= 2 and any("click" in action for action in actions):
        return True
    return False


@dataclass
class StepCircuitBreaker:
    """Bound retries by endpoint/error signature and force a fresh AI decision."""

    observations: Counter = field(default_factory=Counter)
    blocked_decisions: Counter = field(default_factory=Counter)

    def review(self, browser_state: Any, model_output: Any, action_model: Any) -> str | None:
        try:
            dom_text = browser_state.dom_state.llm_representation()
        except Exception:
            return None

        error = detect_hard_error(dom_text)
        if error is None:
            return None

        endpoint = endpoint_key(getattr(browser_state, "url", ""))
        signature = (*endpoint, error)
        self.observations[signature] += 1
        seen = self.observations[signature]

        # CAPTCHA/rate-limit pages should never be probed repeatedly.
        terminal_safety_error = error in {"captcha", "rate_limit"}
        retrying = is_retry_decision(getattr(browser_state, "url", ""), model_output, endpoint_seen=seen)
        if not terminal_safety_error and not retrying:
            return None

        self.blocked_decisions[signature] += 1
        blocked = self.blocked_decisions[signature]
        reason = (
            f"Circuit breaker: {error} observed {seen} time(s) on {endpoint[0]}{endpoint[1]}. "
            "Do not knock on this endpoint again."
        )

        should_finish = terminal_safety_error or blocked >= 2
        if should_finish:
            payload = {
                "done": {
                    "text": reason + " Stopped without claiming success; report the obstacle and any grounded partial results.",
                    "success": False,
                }
            }
        else:
            # A no-op gives the model one fresh observation cycle. The modified
            # memory/goal makes the next LLM decision explicitly choose a new
            # endpoint/query/site or finish, rather than executing this retry.
            payload = {"wait": {"seconds": 1}}
            model_output.memory = ((getattr(model_output, "memory", "") or "") + "\n" + reason).strip()
            model_output.next_goal = (
                "Choose a genuinely different endpoint/query/site now, or call done(success=false). "
                "Do not retry/reload the blocked endpoint."
            )

        model_output.action = [action_model.model_validate(payload)]
        return reason
