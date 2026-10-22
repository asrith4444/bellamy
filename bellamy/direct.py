"""Direct form actions: no AI. The caller (Mark) sends exact steps — a CSS selector, a kind and
a value — and Bellamy performs them in the open browser with real clicks and keystrokes, then
reads back what every field shows. A form that takes the AI agent ten minutes takes seconds.

Step kinds:
  text    {"selector", "value"}           type into an input or textarea
  select  {"selector", "value"}           a native <select> or a searchable combobox (react-select)
  check   {"selector"}                    tick a checkbox or radio button
  upload  {"selector", "path"}            attach a local file to a file input
Every step also carries "label" (reported back) and "required", and may carry "fallback": other
step fields to use when its selector is not on the page.

Nothing here clicks a submit button except submit(), which Mark calls only after its own check.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

# Read one field's visible value: the input's value, a <select>'s chosen option, a combobox's
# selected chip, a checkbox's state, or the attached file's name.
_READ = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return JSON.stringify({found: false});
  let value = "";
  if (el.type === "file") {
    const box = el.closest(".field-wrapper, .file-upload, [class*=upload]") || el.parentElement;
    const name = el.files && el.files[0] ? el.files[0].name : "";
    value = name && box && box.innerText.includes(name) ? name : (name ? "attached:" + name : "");
  } else if (el.type === "checkbox" || el.type === "radio") {
    const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    value = el.checked ? ((label && label.innerText.trim()) || "checked") : "";
  } else if (el.tagName === "SELECT") {
    value = el.selectedIndex >= 0 ? el.options[el.selectedIndex].text.trim() : "";
  } else if (el.getAttribute("role") === "combobox") {
    const control = el.closest("[class*=control]") || el.parentElement;
    const chips = control ? [...control.querySelectorAll("[class*=single-value], [class*=singleValue], [class*=multi-value__label], [class*=multiValue] > div:first-child")] : [];
    value = chips.map(c => c.innerText.trim()).filter(Boolean).join(", ");
  } else {
    value = el.value || "";
  }
  return JSON.stringify({found: true, value, invalid: el.getAttribute("aria-invalid") === "true"});
}"""

# The options a combobox shows right now (its listbox, else any visible option on the page).
_OPTIONS = r"""(selector) => {
  const el = document.querySelector(selector);
  const listId = el && (el.getAttribute("aria-controls") || el.getAttribute("aria-owns"));
  let options = listId && document.getElementById(listId) ? [...document.getElementById(listId).querySelectorAll("[role=option]")] : [];
  if (!options.length) options = [...document.querySelectorAll("[role=listbox] [role=option], [role=option]")];
  options = options.filter(o => o.offsetParent !== null);
  options.forEach((o, i) => o.setAttribute("data-bellamy-option", String(i)));
  return JSON.stringify(options.map(o => o.innerText.replace(/\s+/g, " ").trim()));
}"""

# Required fields on the page that are still empty and that no step covered.
_EMPTY_REQUIRED = r"""(covered) => {
  const seen = new Set(covered);
  const out = [];
  for (const el of document.querySelectorAll("input, textarea, select, [role=group][aria-required=true]")) {
    if ((el.offsetParent === null && el.type !== "file") || el.disabled || el.getAttribute("aria-disabled") === "true") continue;
    const required = el.required || el.getAttribute("aria-required") === "true";
    if (!required || !el.id || seen.has("#" + el.id) || seen.has('[id="' + el.id + '"]')) continue;
    if (el.type === "hidden" || el.type === "file") continue;
    let filled = !!el.value;
    if (el.getAttribute("role") === "combobox") {
      const control = el.closest("[class*=control]");
      filled = !!(control && control.querySelector("[class*=single-value], [class*=multi-value]"));
    }
    if (el.type === "checkbox" || el.type === "radio") {
      filled = [...document.getElementsByName(el.name)].some(x => x.checked);
    }
    if (filled) continue;
    const label = document.querySelector('label[for="' + el.id + '"]');
    out.push((label ? label.innerText : el.getAttribute("aria-label") || el.id).replace(/\*/g, "").trim());
  }
  return JSON.stringify([...new Set(out)]);
}"""

# Visible error messages and an active (visible) human-verification challenge.
_PAGE_STATE = r"""() => {
  const errors = [...document.querySelectorAll("[id$=-error], [class*=error-message], [class*=helper-text--error], [role=alert]")]
    .filter(e => e.offsetParent !== null && e.innerText.trim()).map(e => e.innerText.replace(/\s+/g, " ").trim());
  const challenge = [...document.querySelectorAll("iframe")].some(f => {
    const src = f.src || "";
    const box = f.getBoundingClientRect();
    return /recaptcha\/(api2|enterprise)\/bframe|hcaptcha\.com.*challenge|challenges\.cloudflare/.test(src) && box.width > 50 && box.height > 50;
  });
  const text = document.body ? document.body.innerText.replace(/\s+/g, " ") : "";
  return JSON.stringify({url: location.href, errors: [...new Set(errors)].slice(0, 10), challenge, text: text.slice(0, 4000)});
}"""

CONFIRMATION = re.compile(r"thank you for (?:applying|your application|your interest)|application (?:has been )?(?:submitted|received)"
                          r"|we(?:'ve| have) received your application|successfully submitted", re.I)


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9+]+", "", text.lower())


def best_option(value: str, options: list[str], search: bool = False) -> int | None:
    """Index of the option matching `value`: exact, then ignoring spaces and punctuation, then prefix.
    For a search box (a city lookup), the first result starting with the value's first part."""
    if not options:
        return None
    for i, option in enumerate(options):
        if option.strip().lower() == value.strip().lower():
            return i
    target = squash(value)
    for i, option in enumerate(options):
        if squash(option) == target:
            return i
    for i, option in enumerate(options):
        if target and (squash(option).startswith(target) or target.startswith(squash(option))):
            return i
    head = squash(value.split(",")[0])
    if search and head:
        hit = next((i for i, option in enumerate(options) if squash(option).startswith(head)), None)
        if hit is not None:
            return hit
        # "University of Colorado Boulder" vs "University of Colorado at Boulder": same words.
        words = _words(value)
        return next((i for i, option in enumerate(options) if words and words <= _words(option)), None)
    return None


_FILLER = {"of", "the", "at", "and", "in", "a", "an"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _FILLER}


def search_text(value: str) -> str:
    """What to type into a searchable dropdown: the start of the option, without dial codes."""
    text = re.sub(r"\s*\+\d[\d\s-]*$", "", value).strip()
    return text[:40] or value[:40]


async def _js(page, function: str, *args: Any) -> Any:
    raw = await page.evaluate(function, *args)
    return json.loads(raw) if raw else None


async def _element(page, selector: str):
    elements = await page.get_elements_by_css_selector(selector)
    return elements[0] if elements else None


async def settle(page, timeout: float = 10.0) -> None:
    """Wait until the page has finished loading its code. Text typed before a form's script has
    started is wiped when it does."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await page.evaluate("() => document.readyState") == "complete":
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(1.0)


async def wait_for(page, selector: str, timeout: float = 20.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await page.evaluate("(s) => !!document.querySelector(s)", selector) in ("True", "true"):
            return True
        await asyncio.sleep(0.4)
    return False


# Mark the file input's field box and its "Attach" button, so both can be found after the
# widget redraws itself. Some upload widgets only set up their uploader when Attach is clicked.
_TAG_UPLOAD = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return JSON.stringify({found: false});
  let box = el.parentElement;
  while (box && box !== document.body && !box.querySelector("button")) box = box.parentElement;
  const wrapper = el.closest(".field-wrapper, .file-upload, [class*=upload]") || box || el.parentElement;
  wrapper.setAttribute("data-bellamy-upload", "1");
  const button = box && box !== document.body ? box.querySelector("button") : null;
  if (button) button.setAttribute("data-bellamy-attach", "1");
  return JSON.stringify({found: true, button: !!button});
}"""
_UPLOADED = r"""(name) => {
  const box = document.querySelector("[data-bellamy-upload]");
  const text = box ? box.innerText : "";
  const input = box && box.querySelector("input[type=file]");
  const failed = /error|failed|cannot|could not/i.test(text);
  return JSON.stringify({shown: text.includes(name), attached: !!(input && input.files && input.files.length), failed, text: text.replace(/\s+/g, " ").slice(0, 200)});
}"""


async def _upload(page, selector: str, path: str) -> tuple[str, str]:
    """Attach a file the way a person does: click Attach (its file dialog is intercepted), then
    hand the browser the file. Returns (what the field shows, note)."""
    name = Path(path).name
    tagged = await _js(page, _TAG_UPLOAD, selector) or {}
    if not tagged.get("found"):
        return "", "not on the page"
    session_id = await page.session_id
    client = page._client
    await client.send.Page.setInterceptFileChooserDialog(params={"enabled": True}, session_id=session_id)
    try:
        if tagged.get("button"):
            await (await _element(page, '[data-bellamy-attach="1"]')).click()
            await asyncio.sleep(0.5)
        element = await _element(page, selector) or await _element(page, "[data-bellamy-upload] input[type=file]")
        if element is None:
            return "", "file input disappeared"
        await client.send.DOM.setFileInputFiles(
            params={"files": [path], "backendNodeId": element._backend_node_id}, session_id=element._session_id)
        for _ in range(20):  # the form uploads it and then shows the file name
            await asyncio.sleep(0.5)
            state = await _js(page, _UPLOADED, name) or {}
            if state.get("shown"):
                return name, ""
            if state.get("failed"):
                return "", f"upload failed: {state.get('text')}"
        return "", "the form never showed the file name"
    finally:
        await client.send.Page.setInterceptFileChooserDialog(params={"enabled": False}, session_id=session_id)


_PICK_OPTION = r"""(i) => { const o = document.querySelector('[data-bellamy-option="' + i + '"]');
  if (!o) return false; o.scrollIntoView({block: "nearest"}); o.click(); return true; }"""
_IS_CHECKED = "(s) => !!(document.querySelector(s) && document.querySelector(s).checked)"
# Styled checkboxes often hide the real box under a drawing: click its label, then the box itself.
_CLICK_LABEL = r"""(s) => { const el = document.querySelector(s); const label = el && el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
  if (label) { label.click(); return true; } return false; }"""
_CLICK_SELF = "(s) => { document.querySelector(s).click(); return true; }"


async def _tick(page, selector: str) -> str:
    for attempt in range(3):
        if await page.evaluate(_IS_CHECKED, selector) in ("True", "true"):
            return ""
        if attempt == 0:
            await (await _element(page, selector)).click()
        else:
            await page.evaluate(_CLICK_LABEL if attempt == 1 else _CLICK_SELF, selector)
        await asyncio.sleep(0.3)
    return "" if await page.evaluate(_IS_CHECKED, selector) in ("True", "true") else "would not tick"


async def _choose(page, selector: str, value: str, search: bool = False) -> tuple[str, str]:
    """Pick `value` in a native select or a searchable combobox: (option chosen, note)."""
    kind = await page.evaluate("(s) => { const e = document.querySelector(s); return e ? e.tagName + ':' + (e.getAttribute('role') || '') : ''; }", selector)
    element = await _element(page, selector)
    if element is None:
        return "", "not on the page"
    if kind.startswith("SELECT"):
        options = await _js(page, "(s) => JSON.stringify([...document.querySelector(s).options].map(o => o.text.trim()))", selector)
        index = best_option(value, options or [], search)
        if index is None:
            return "", f"no option matches {value!r}"
        await element.select_option(options[index])
        return options[index], ""
    for typed in (search_text(value), ""):
        await element.click()
        await asyncio.sleep(0.2)
        if typed:
            await element.fill(typed)
        index, options = None, []
        for _ in range(10 if search else 4):  # a search box loads its results over the network
            await asyncio.sleep(0.5)
            options = await _js(page, _OPTIONS, selector) or []
            index = best_option(value, options, search)
            if index is not None:
                break
        if index is not None:
            # Click the option element itself: a click by screen position can land on whatever
            # moved under it (an "Add another" button) once the list redraws.
            picked = await page.evaluate(_PICK_OPTION, str(index))
            if picked in ("True", "true"):
                await asyncio.sleep(0.3)
                return options[index], ""
        await page.press("Escape")
    return "", f"no option matches {value!r}"


async def fill(page, steps: list[dict]) -> dict:
    """Perform every step, then report what each field shows (Bellamy's FillReport shape)."""
    fields, unfilled, resume_uploaded = [], [], False
    for step in steps:
        if step.get("fallback") and not await wait_for(page, step["selector"], timeout=0.6):
            step = {**step, **step["fallback"]}  # e.g. a checkbox question drawn as a multi-select dropdown
        selector, kind, label = step["selector"], step["kind"], step.get("label") or step["selector"]
        note, chosen, uploaded = "", "", ""
        try:
            if not await wait_for(page, selector, timeout=3 if step.get("required") else 0.6):
                note = "not on the page"
            elif kind == "text":
                for _ in range(3):  # retype a value the page wiped
                    await (await _element(page, selector)).fill(str(step["value"]))
                    shown = await page.evaluate("(s) => document.querySelector(s).value", selector)
                    if squash(shown) == squash(str(step["value"])):
                        break
                    await asyncio.sleep(0.7)
            elif kind == "select":
                chosen, note = await _choose(page, selector, str(step["value"]), bool(step.get("search")))
                if note and step.get("otherwise"):  # e.g. a school missing from the form's list: "Other"
                    chosen, note = await _choose(page, selector, str(step["otherwise"]))
            elif kind == "check":
                note = await _tick(page, selector)
            elif kind == "upload":
                path = str(Path(step["path"]).expanduser())
                if not Path(path).is_file():
                    note = f"file not found: {path}"
                else:
                    uploaded, note = await _upload(page, selector, path)
            else:
                note = f"unknown step kind {kind!r}"
        except Exception as exc:  # one bad field must not lose the rest of the form
            note = f"{type(exc).__name__}: {exc}"
        shown = await _js(page, _READ, selector) or {}
        value = str(shown.get("value") or "")
        if kind == "upload":
            value, shown = uploaded, {}
        elif chosen and value and squash(value) in squash(chosen):
            value = chosen  # a chip may show only part of the option, e.g. "+1" for "United States +1"
        ok = not note and bool(value) and not shown.get("invalid")
        if kind == "upload" and ok:
            resume_uploaded = resume_uploaded or step.get("name", "resume") == "resume"
        if not ok and step.get("required"):
            unfilled.append(label)
        status = "filled" if ok else "failed" if step.get("required") else "skipped"  # optional: leave it
        fields.append({"label": label, "value": value, "status": status,
                       "note": note or ("shows nothing" if not value else "marked invalid" if shown.get("invalid") else "")})
    covered = [s["selector"] for s in steps]
    extra = await _js(page, _EMPTY_REQUIRED, covered) or []
    state = await _js(page, _PAGE_STATE) or {}
    blockers = ["captcha: a verification challenge is showing"] if state.get("challenge") else []
    unfilled += [label for label in extra if label not in unfilled]
    return {"fields": fields, "resume_uploaded": resume_uploaded, "unfilled_required": unfilled,
            "blockers": blockers, "ready_to_submit": not unfilled and not blockers}


async def submit(page, selector: str, wait: float = 30.0) -> dict:
    """Click the form's submit button once and wait for a confirmation, an error or a challenge."""
    button = await _element(page, selector)
    if button is None:
        return {"submitted": False, "confirmation_text": "", "blockers": [f"submit button {selector!r} not found"]}
    before = (await _js(page, _PAGE_STATE) or {}).get("url", "")
    await button.click()
    deadline = asyncio.get_running_loop().time() + wait
    state: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        try:
            state = await _js(page, _PAGE_STATE) or {}
        except Exception:
            continue  # the page is navigating
        match = CONFIRMATION.search(state.get("text", ""))
        if match or "confirmation" in state.get("url", "") and state.get("url") != before:
            sentence = match.group(0) if match else "confirmation page"
            return {"submitted": True, "confirmation_text": sentence, "blockers": [], **_evidence(state)}
        if state.get("challenge"):
            return {"submitted": False, "confirmation_text": "", "blockers": ["captcha: a verification challenge appeared after submit"],
                    **_evidence(state)}
        if state.get("errors") and asyncio.get_running_loop().time() > deadline - wait + 4:
            return {"submitted": False, "confirmation_text": "", "blockers": [f"form error: {e}" for e in state["errors"]],
                    **_evidence(state)}
    return {"submitted": False, "confirmation_text": "", "blockers": ["no confirmation seen after submit"], **_evidence(state)}


def _evidence(state: dict) -> dict:
    """Where the page ended up and what it said: the record for an outcome Mark can't confirm."""
    return {"url": state.get("url", ""), "page_text": (state.get("text") or "")[-1500:]}
