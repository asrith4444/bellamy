"""Direct form actions: no AI. The caller (Mark) sends exact steps — a CSS selector, a kind and
a value — and Bellamy performs them in the open browser with real clicks and keystrokes, then
reads back what every field shows. A form that takes the AI agent ten minutes takes seconds.

Step kinds:
  text    {"selector", "value"}           type into an input or textarea
  select  {"selector", "value"}           a native <select> or a searchable combobox (react-select)
  check   {"selector"}                    tick a checkbox or radio button
  upload  {"selector", "path"}            attach a local file to a file input ("shown_in": where the
                                          form shows the file's name once it has it)
  pick    {"selector", "value"|"values"}  choose an answer inside one question's box, whatever draws
                                          it: Yes/No toggle buttons, a radio or checkbox group (by
                                          label), or a dropdown/typeahead ("search" for a city lookup)
Every step also carries "label" (reported back) and "required", and may carry "fallback": other
step fields to use when its selector is not on the page, and "close": press Escape afterwards (a
date picker left open covers the fields below it).

Nothing here clicks a submit button except submit() and enter_code(), which Mark calls only after
its own check. When the site emails a code after Submit (Greenhouse's "Security code"), submit()
reports code_required and leaves the form open; enter_code() types the code and submits again.
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
    // A typeahead that keeps the choice as its text (Ashby), not react-select (its text box holds only the search).
    if (!value && !el.closest("[class*=control]") && el.getAttribute("aria-expanded") !== "true") value = el.value || "";
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

# One question's box for a "pick" step: what draws its choices, and their labels. Each choice is
# tagged data-bellamy-choice="0", "1", ... (the dropdown: "combo") so it can be clicked by tag.
_PICK_SCAN = r"""(selector) => {
  const box = document.querySelector(selector);
  if (!box) return JSON.stringify({found: false});
  document.querySelectorAll("[data-bellamy-choice]").forEach(e => e.removeAttribute("data-bellamy-choice"));
  const tidy = (t) => (t || "").replace(/\s+/g, " ").trim();
  const buttons = [...box.querySelectorAll("button[aria-pressed]")];
  if (buttons.length) {
    buttons.forEach((b, i) => b.setAttribute("data-bellamy-choice", String(i)));
    return JSON.stringify({found: true, type: "buttons", options: buttons.map(b => tidy(b.innerText))});
  }
  const labelOf = (i) => i.id && document.querySelector('label[for="' + CSS.escape(i.id) + '"]');
  const inputs = [...box.querySelectorAll("input[type=radio], input[type=checkbox]")].filter(labelOf);
  if (inputs.length) {
    inputs.forEach((x, i) => x.setAttribute("data-bellamy-choice", String(i)));
    return JSON.stringify({found: true, type: "inputs", options: inputs.map(x => tidy(labelOf(x).innerText))});
  }
  const combo = box.querySelector("[role=combobox], select");
  if (combo) { combo.setAttribute("data-bellamy-choice", "combo"); return JSON.stringify({found: true, type: "combo"}); }
  return JSON.stringify({found: true, type: "none"});
}"""

# What a question's box shows as chosen: pressed buttons, ticked boxes (their labels), or the dropdown's choice.
_READ_CHOICE = r"""(selector) => {
  const box = document.querySelector(selector);
  if (!box) return JSON.stringify({found: false});
  const tidy = (t) => (t || "").replace(/\s+/g, " ").trim();
  const pressed = [...box.querySelectorAll('button[aria-pressed="true"]')].map(b => tidy(b.innerText));
  const ticked = [...box.querySelectorAll("input[type=radio], input[type=checkbox]")].filter(i => i.checked && i.id).map(i => {
    const label = document.querySelector('label[for="' + CSS.escape(i.id) + '"]');
    return label ? tidy(label.innerText) : "checked";
  });
  let value = [...pressed, ...ticked].join(", ");
  const combo = box.querySelector("[role=combobox]");
  const select = box.querySelector("select");
  if (!value && combo) {
    const control = combo.closest("[class*=control]") || combo.parentElement;
    const chips = control ? [...control.querySelectorAll("[class*=single-value], [class*=singleValue], [class*=multi-value__label]")] : [];
    value = chips.map(c => tidy(c.innerText)).filter(Boolean).join(", ") || (combo.getAttribute("aria-expanded") !== "true" ? combo.value || "" : "");
  }
  if (!value && select && select.selectedIndex >= 0) value = tidy(select.options[select.selectedIndex].text);
  return JSON.stringify({found: true, value, invalid: !!box.querySelector('[aria-invalid="true"]')});
}"""
_PRESSED = "(s) => { const b = document.querySelector(s); return !!(b && b.getAttribute('aria-pressed') === 'true'); }"

# A success panel some forms show instead of a thank-you sentence (Ashby's can carry the company's own words).
SUCCESS_PANELS = ".ashby-application-form-success-container"

# Visible error messages and an active (visible) human-verification challenge.
_PAGE_STATE = r"""() => {
  const errors = [...document.querySelectorAll("[id$=-error], [class*=error-message], [class*=helper-text--error], [role=alert]")]
    .filter(e => e.offsetParent !== null && e.innerText.trim()).map(e => e.innerText.replace(/\s+/g, " ").trim());
  const panel = [...document.querySelectorAll(""" + json.dumps(SUCCESS_PANELS) + r""")].find(e => e.offsetParent !== null && e.innerText.trim());
  const success = panel ? panel.innerText.replace(/\s+/g, " ").trim().slice(0, 300) : "";
  // reCAPTCHA keeps its challenge frame on the page all along, full size but hidden and parked
  // at top: -10000px (Ashby); it counts only once it is actually shown.
  const shown = (f) => {
    const box = f.getBoundingClientRect();
    if (box.width <= 50 || box.height <= 50 || box.bottom + window.scrollY < 0 || getComputedStyle(f).visibility === "hidden") return false;
    for (let e = f; e && e !== document.body; e = e.parentElement) {
      const style = getComputedStyle(e);
      if (style.display === "none" || parseFloat(style.opacity) === 0) return false;
    }
    return true;
  };
  const challenge = [...document.querySelectorAll("iframe")].some(f =>
    /recaptcha\/(api2|enterprise)\/bframe|hcaptcha\.com.*challenge|challenges\.cloudflare/.test(f.src || "") && shown(f));
  const all = document.body ? document.body.innerText.replace(/\s+/g, " ") : "";
  // The start (headings, a confirmation page) and the end (the form, its errors, an inline thank-you).
  const text = all.length > 5000 ? all.slice(0, 1500) + " … " + all.slice(-3500) : all;
  return JSON.stringify({url: location.href, errors: [...new Set(errors)].slice(0, 10), challenge, text, success});
}"""

# The submit button: still on the page, and whether it shows the form is busy sending.
_BUTTON_STATE = r"""(selector) => {
  const b = document.querySelector(selector);
  if (!b || b.offsetParent === null) return JSON.stringify({found: false, busy: false});
  const busy = b.disabled || b.getAttribute("aria-disabled") === "true" || b.getAttribute("aria-busy") === "true"
    || /submitting|sending|loading|please wait/i.test(b.innerText || b.value || "");
  return JSON.stringify({found: true, busy});
}"""
_SCROLL_TO = "(s) => { const e = document.querySelector(s); if (e) e.scrollIntoView({block: 'center'}); return !!e; }"
_CLICK_JS = "(s) => { const e = document.querySelector(s); if (!e) return false; e.scrollIntoView({block: 'center'}); e.click(); return true; }"
NUDGE_AFTER = 8.0  # seconds without any reaction before Submit is pressed once more
UNCHANGED = "not submitted: the form did not react to Submit (still open, nothing sent)"

CONFIRMATION = re.compile(r"thank you for (?:applying|your application|your interest)|application (?:has been )?(?:submitted|received)"
                          r"|we(?:'ve| have) received your application|successfully submitted", re.I)

# A code the site emailed and wants typed in before it takes the application. Greenhouse shows
# "Security code" as eight one-letter boxes (security-input-0..7) in #email-verification, after
# Submit, and keeps the application until the code is entered and Submit is pressed again.
# Elsewhere: a visible, enabled code box on a page that talks about a code it sent.
# The boxes are tagged data-bellamy-code="0", "1", ... so enter_code can type into each.
_CODE_PROMPT = r"""() => {
  const shown = (e) => e && e.offsetParent !== null && !e.disabled;
  const asks = /(verification|security|one[- ]time|confirmation|access) code|enter the code|code (?:was |has been |we )?sent/i;
  const tidy = (t) => (t || "").replace(/\s+/g, " ").trim();
  let boxes = [...document.querySelectorAll('[id^="security-input-"]')].filter(shown);
  let scope = document.getElementById("email-verification");
  if (!boxes.length) {
    const text = document.body ? document.body.innerText : "";
    if (!asks.test(text)) return JSON.stringify({found: false});
    boxes = [...document.querySelectorAll("input")].filter(i => shown(i) && /^(text|tel|number|)$/.test(i.getAttribute("type") || "")
      && /code|otp|verif|security|passcode/i.test([i.id, i.name, i.autocomplete, i.getAttribute("aria-label"), i.placeholder].join(" ")));
    scope = boxes[0] ? (boxes[0].closest("fieldset, form, [role=dialog], section") || boxes[0].parentElement) : null;
  }
  if (!boxes.length) return JSON.stringify({found: false});
  document.querySelectorAll("[data-bellamy-code]").forEach(e => e.removeAttribute("data-bellamy-code"));
  boxes.forEach((b, i) => b.setAttribute("data-bellamy-code", String(i)));
  let prompt = tidy(scope && scope.innerText);
  if (!asks.test(prompt)) {
    const body = tidy(document.body.innerText);
    const at = body.search(asks);
    prompt = at >= 0 ? body.slice(Math.max(0, at - 120), at + 180) : prompt;
  }
  const errorEl = document.getElementById("email-verification-error")
    || (scope && [...scope.querySelectorAll("[role=alert], [id$=-error], [class*=error]")].find(e => e.offsetParent !== null && e.innerText.trim()));
  const single = boxes.length === 1 && boxes[0].maxLength > 3 && boxes[0].maxLength < 13 ? boxes[0].maxLength : 0;
  return JSON.stringify({found: true, boxes: boxes.length, length: boxes.length > 1 ? boxes.length : single,
                         prompt: prompt.slice(0, 300), error: tidy(errorEl && errorEl.innerText).slice(0, 200),
                         value: boxes.map(b => b.value || "").join("")});
}"""

# The button that sends the code: the form's submit button, else a Verify/Submit/Continue
# button next to the code boxes. Tagged data-bellamy-code-submit; reports whether it is enabled
# (Greenhouse keeps Submit disabled until all eight boxes hold a character).
_CODE_BUTTON = r"""(selector) => {
  const shown = (e) => e && e.offsetParent !== null;
  const first = document.querySelector('[data-bellamy-code="0"]');
  let button = [...document.querySelectorAll(selector)].find(shown);
  if (!button && first) {
    const scope = first.closest("form, [role=dialog], fieldset, section") || document.body;
    button = [...scope.querySelectorAll("button, input[type=submit]")]
      .find(b => shown(b) && /verify|submit|continue|confirm/i.test(b.innerText || b.value || ""));
  }
  document.querySelectorAll("[data-bellamy-code-submit]").forEach(e => e.removeAttribute("data-bellamy-code-submit"));
  if (!button) return JSON.stringify({found: false});
  button.setAttribute("data-bellamy-code-submit", "1");
  return JSON.stringify({found: true, enabled: !button.disabled && button.getAttribute("aria-disabled") !== "true"});
}"""

CODE_ASKED = "verification code: the site emailed a code and takes the application only once it is entered"


def code_result(prompt: dict, error: str = "", state: dict | None = None) -> dict:
    """A SubmitResult for a page waiting for an emailed code. `error`: why the last code failed."""
    return {"submitted": False, "confirmation_text": "",
            "blockers": [f"verification code rejected: {error}" if error else CODE_ASKED],
            "code_required": True, "code_prompt": str(prompt.get("prompt") or "")[:300],
            "code_length": int(prompt.get("length") or 0), "code_error": error, **_evidence(state or {})}


def code_length(prompt_text: str) -> int:
    """'enter the 8-character code' -> 8. 0 when the text doesn't say."""
    match = re.search(r"\b(\d{1,2})[- ](?:character|digit|letter|symbol)", prompt_text or "", re.I)
    return int(match.group(1)) if match else 0


async def code_prompt(page) -> dict:
    """The emailed-code step if the page shows one: {"found", "boxes", "length", "prompt", "error", "value"}."""
    state = await _js(page, _CODE_PROMPT) or {}
    if state.get("found") and not state.get("length"):
        state["length"] = code_length(state.get("prompt", ""))
    return state


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
_UPLOADED = r"""(name, shownIn) => {
  // The tagged box, or (when the widget redrew it) the field's own box the step names.
  const tagged = document.querySelector("[data-bellamy-upload]");
  const named = shownIn ? document.querySelector(shownIn) : null;
  const box = tagged && tagged.innerText.includes(name) ? tagged : (named || tagged);
  const text = box ? box.innerText : "";
  const input = box && box.querySelector("input[type=file]");
  const failed = /error|failed|cannot|could not/i.test(text);
  return JSON.stringify({shown: text.includes(name), attached: !!(input && input.files && input.files.length), failed, text: text.replace(/\s+/g, " ").slice(0, 200)});
}"""


async def _upload(page, selector: str, path: str, shown_in: str = "") -> tuple[str, str]:
    """Attach a file the way a person does: click Attach (its file dialog is intercepted), then
    hand the browser the file. Returns (what the field shows, note). `shown_in`: the field's box,
    for widgets that redraw themselves once they have the file."""
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
            state = await _js(page, _UPLOADED, name, shown_in) or {}
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


async def _press(page, selector: str) -> str:
    """Press a toggle button (Ashby's Yes/No) until the page shows it pressed."""
    for attempt in range(3):
        if await page.evaluate(_PRESSED, selector) in ("True", "true"):
            return ""
        if attempt == 0:
            await (await _element(page, selector)).click()
        else:
            await page.evaluate(_CLICK_SELF, selector)
        await asyncio.sleep(0.3)
    return "" if await page.evaluate(_PRESSED, selector) in ("True", "true") else "would not press"


async def pick(page, selector: str, values: list[str], search: bool = False) -> tuple[str, str]:
    """Choose `values` inside one question's box (`selector`): its Yes/No buttons, its radio or
    checkbox group (matched by label), or its dropdown. Returns (what was chosen, note)."""
    scan = await _js(page, _PICK_SCAN, selector) or {}
    if not scan.get("found"):
        return "", "not on the page"
    if not values:
        return "", "no answer to choose"
    kind, options = scan.get("type"), scan.get("options") or []
    if kind == "combo":
        if len(values) != 1:
            return "", "the dropdown takes one answer"
        return await _choose(page, '[data-bellamy-choice="combo"]', values[0], search)
    if kind not in ("buttons", "inputs") or not options:
        return "", "no choices in this question"
    indexes = [best_option(value, options) for value in values]
    missing = [value for value, index in zip(values, indexes) if index is None]
    if missing:
        return "", f"no option matches {missing[0]!r} (options: {', '.join(options[:8])})"
    for index in indexes:
        target = f'[data-bellamy-choice="{index}"]'
        note = await (_press(page, target) if kind == "buttons" else _tick(page, target))
        if note:
            return "", f"{options[index]}: {note}"
    return ", ".join(options[i] for i in indexes), ""


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
            elif kind == "pick":
                values = [str(v) for v in step.get("values") or [step.get("value")] if v is not None]
                chosen, note = await pick(page, selector, values, bool(step.get("search")))
            elif kind == "upload":
                path = str(Path(step["path"]).expanduser())
                if not Path(path).is_file():
                    note = f"file not found: {path}"
                else:
                    uploaded, note = await _upload(page, selector, path, step.get("shown_in") or "")
            else:
                note = f"unknown step kind {kind!r}"
            if step.get("close"):
                await page.press("Escape")
                await asyncio.sleep(0.2)
        except Exception as exc:  # one bad field must not lose the rest of the form
            note = f"{type(exc).__name__}: {exc}"
        shown = await _js(page, _READ_CHOICE if kind == "pick" else _READ, selector) or {}
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


async def submit(page, selector: str, wait: float = 45.0) -> dict:
    """Click the form's submit button once and wait for a confirmation, an error, a challenge or
    a request for an emailed code (then code_required is set and the form stays open for enter_code)."""
    button = await _element(page, selector)
    if button is None:
        return {"submitted": False, "confirmation_text": "", "blockers": [f"submit button {selector!r} not found"]}
    before = (await _js(page, _PAGE_STATE) or {}).get("url", "")
    await page.evaluate(_SCROLL_TO, selector)  # a click off screen, or under the reCAPTCHA badge, can miss
    await asyncio.sleep(0.4)
    await button.click()
    return await _outcome(page, before, wait, selector=selector)


async def enter_code(page, code: str, selector: str = "button[type=submit]", wait: float = 30.0) -> dict:
    """Type an emailed code into the code step, press submit once and wait for the outcome.
    A code the site turns down comes back as code_required with its message in code_error."""
    prompt = await code_prompt(page)
    if not prompt.get("found"):
        return {"submitted": False, "confirmation_text": "", "blockers": ["the code field is not on the page"]}
    boxes = int(prompt.get("boxes") or 1)
    if boxes > 1 and len(code) != boxes:
        return code_result(prompt, f"the code has {len(code)} characters; the form has {boxes} boxes")
    for i, part in enumerate(list(code) if boxes > 1 else [code]):
        box = await _element(page, f'[data-bellamy-code="{i}"]')
        if box is None:
            return code_result(prompt, "the code boxes changed while typing")
        await box.fill(part)
        await asyncio.sleep(0.15)
    if (await code_prompt(page)).get("value", "") != code:
        return code_result(prompt, "the code did not stay in the boxes")
    found: dict = {}
    for _ in range(10):  # Submit turns on once every box holds a character
        found = await _js(page, _CODE_BUTTON, selector) or {}
        if not found.get("found") or found.get("enabled"):
            break
        await asyncio.sleep(0.5)
    if not found.get("found"):
        return code_result(prompt, f"no button to send the code ({selector!r} not found)")
    if not found.get("enabled"):
        return code_result(prompt, "the submit button stayed disabled after typing the code")
    before = (await _js(page, _PAGE_STATE) or {}).get("url", "")
    await (await _element(page, '[data-bellamy-code-submit="1"]')).click()
    # Submit after the code has gone unheard on Greenhouse (the boxes full, the button enabled, nothing sent):
    # pressed again when the page shows no reaction.
    return await _outcome(page, before, wait, code_sent=True, selector='[data-bellamy-code-submit="1"]')


async def _outcome(page, before: str, wait: float, *, code_sent: bool = False, selector: str | None = None) -> dict:
    """After a submit click: wait for a confirmation, a challenge, a request for an emailed code
    (or, once a code was sent, the site's verdict on it), or form errors.

    With `selector` (the first submit), a page that shows no reaction at all (the button never
    busy, nothing new on the page) gets Submit pressed once more after NUDGE_AFTER seconds; still
    nothing means the form was never sent: form_unchanged is set and the form stays as it was."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    state: dict = {}
    prompt: dict = {}
    reacted, nudged = selector is None, False
    while loop.time() < start + wait:
        await asyncio.sleep(0.5)
        try:
            state = await _js(page, _PAGE_STATE) or {}
            prompt = await code_prompt(page)
            button = await _js(page, _BUTTON_STATE, selector) if selector else None
        except Exception:
            reacted = True  # the page is navigating
            continue
        match = CONFIRMATION.search(state.get("text", ""))
        if match or state.get("success") or "confirmation" in state.get("url", "") and state.get("url") != before:
            return {"submitted": True, "confirmation_text": confirmation_sentence(state, match), "blockers": [], **_evidence(state)}
        if state.get("challenge"):
            return {"submitted": False, "confirmation_text": "", "blockers": ["captcha: a verification challenge appeared after submit"],
                    **_evidence(state)}
        if prompt.get("found"):
            if not code_sent:
                return code_result(prompt, state=state)
            if prompt.get("error"):
                return code_result(prompt, prompt["error"], state)
            # the code is on its way to the site (unless Submit went unheard: checked below)
        elif state.get("errors") and loop.time() > start + 4:
            return {"submitted": False, "confirmation_text": "", "blockers": [f"form error: {e}" for e in state["errors"]],
                    **_evidence(state)}
        if button is not None and (not button.get("found") or button.get("busy") or state.get("url") != before):
            reacted = True
        if not reacted and not nudged and loop.time() > start + NUDGE_AFTER:
            nudged = True
            await page.evaluate(_CLICK_JS, selector)
    if code_sent and prompt.get("found"):
        unheard = "Submit drew no reaction, even pressed twice" if not reacted else "the site did not take the code"
        return code_result(prompt, prompt.get("error") or unheard, state)
    if not reacted:
        return {"submitted": False, "confirmation_text": "", "blockers": [UNCHANGED], "form_unchanged": True,
                **_evidence(state)}
    return {"submitted": False, "confirmation_text": "", "blockers": ["no confirmation seen after submit"], **_evidence(state)}


def confirmation_sentence(state: dict, match: re.Match | None) -> str:
    """What says the application went through: the thank-you sentence, else the form's own
    success panel (named as such, since a company may word it its own way), else the page's address."""
    if match:
        return match.group(0)
    if state.get("success"):
        return f"application submitted (the form's success panel: {state['success'][:200]})"
    return "confirmation page"


def _evidence(state: dict) -> dict:
    """Where the page ended up and what it said: the record for an outcome Mark can't confirm."""
    return {"url": state.get("url", ""), "page_text": (state.get("text") or "")[-1500:]}
