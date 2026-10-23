"""Direct (no-AI) form filling: option matching, confirmation text and the worker commands."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from bellamy import direct, worker


class OptionTests(unittest.TestCase):
    def test_best_option_prefers_exact_then_loose_then_prefix(self):
        options = ["United States Minor Outlying Islands+1", "United States+1", "Canada+1"]
        self.assertEqual(direct.best_option("United States +1", options), 1)
        self.assertEqual(direct.best_option("united states+1", options), 1)
        self.assertEqual(direct.best_option("No", ["Not sure", "No", "Yes"]), 1)
        self.assertEqual(direct.best_option("United States", ["Canada", "United States of America"]), 1)
        self.assertIsNone(direct.best_option("Mars", ["Canada", "Mexico"]))
        self.assertIsNone(direct.best_option("Yes", []))

    def test_search_boxes_take_the_first_result_for_the_city(self):
        results = ["Austin, Texas, United States", "Austintown, Ohio, United States"]
        self.assertEqual(direct.best_option("Austin, TX", results, search=True), 0)
        self.assertIsNone(direct.best_option("Austin, TX", results))
        schools = ["University of Colorado Denver", "University of Colorado at Boulder"]
        self.assertEqual(direct.best_option("University of Colorado Boulder", schools, search=True), 1)

    def test_search_text_drops_dial_codes(self):
        self.assertEqual(direct.search_text("United States +1"), "United States")
        self.assertEqual(direct.search_text("No"), "No")

    def test_confirmation_words(self):
        self.assertTrue(direct.CONFIRMATION.search("Thank you for applying to GitLab!"))
        self.assertTrue(direct.CONFIRMATION.search("Your application has been submitted."))
        self.assertFalse(direct.CONFIRMATION.search("Submit application"))


class WorkerDirectTests(unittest.TestCase):
    def make(self):
        instance = worker.Worker()
        instance._ensure = mock.AsyncMock()
        instance.browser = mock.MagicMock()
        instance.browser.navigate_to = mock.AsyncMock()
        instance.browser.start = mock.AsyncMock()
        instance.browser.must_get_current_page = mock.AsyncMock(return_value="page")
        return instance

    def test_fill_direct_opens_the_url_and_reports_without_ai_cost(self):
        instance = self.make()
        report = {"fields": [{"label": "First Name", "value": "Jordan", "status": "filled", "note": ""}],
                  "resume_uploaded": True, "unfilled_required": [], "blockers": [], "ready_to_submit": True}
        with mock.patch.object(direct, "wait_for", mock.AsyncMock(return_value=True)), \
                mock.patch.object(direct, "settle", mock.AsyncMock()), \
                mock.patch.object(direct, "fill", mock.AsyncMock(return_value=report)) as fill:
            reply = asyncio.run(instance.handle({"cmd": "fill_direct", "url": "https://x/1", "steps": [{"selector": "#a"}]}))
        instance.browser.navigate_to.assert_awaited_once_with("https://x/1")
        fill.assert_awaited_once_with("page", [{"selector": "#a"}])
        self.assertTrue(reply["result"]["ready_to_submit"] and reply["success"])
        self.assertEqual(reply["usage"]["cost"], 0.0)

    def test_fill_direct_reports_a_form_that_never_loads(self):
        instance = self.make()
        with mock.patch.object(direct, "wait_for", mock.AsyncMock(return_value=False)):
            reply = asyncio.run(instance.handle({"cmd": "fill_direct", "url": "https://x/1", "steps": [{"selector": "#a"}]}))
        self.assertEqual(reply["result"]["blockers"], ["error: the form did not load"])

    def test_submit_direct(self):
        instance = self.make()
        done = {"submitted": True, "confirmation_text": "Thank you for applying", "blockers": []}
        with mock.patch.object(direct, "submit", mock.AsyncMock(return_value=done)) as submit:
            reply = asyncio.run(instance.handle({"cmd": "submit_direct"}))
        submit.assert_awaited_once_with("page", "button[type=submit]", wait=45.0)
        self.assertTrue(reply["result"]["submitted"])

    def test_enter_code_runs_without_ai(self):
        instance = self.make()
        done = {"submitted": True, "confirmation_text": "Thank you for applying", "blockers": []}
        with mock.patch.object(direct, "enter_code", mock.AsyncMock(return_value=done)) as enter:
            reply = asyncio.run(instance.handle({"cmd": "enter_code", "code": "Ab3dE5fG", "selector": "button.go"}))
        enter.assert_awaited_once_with("page", "Ab3dE5fG", "button.go", wait=30.0)
        self.assertTrue(reply["success"] and reply["result"]["submitted"])
        self.assertEqual(reply["usage"]["cost"], 0.0)

    def test_ai_submit_reports_a_code_step_only_when_the_page_shows_one(self):
        instance = self.make()
        said = worker.SubmitResult(blockers=["verification code"], code_required=True)
        instance.run = mock.AsyncMock(return_value=(said, {"cost": 0.01}, False))
        prompt = {"found": True, "boxes": 8, "length": 8, "prompt": "A verification code was sent to you."}
        with mock.patch.object(direct, "code_prompt", mock.AsyncMock(return_value=prompt)):
            shown = asyncio.run(instance.handle({"cmd": "submit"}))["result"]
        self.assertTrue(shown["code_required"])
        self.assertEqual((shown["code_length"], shown["code_prompt"]), (8, "A verification code was sent to you."))
        with mock.patch.object(direct, "code_prompt", mock.AsyncMock(return_value={"found": False})):
            unseen = asyncio.run(instance.handle({"cmd": "submit"}))["result"]
        self.assertFalse(unseen["code_required"])  # the model's word alone is not a code box


class CodePage:
    """A Greenhouse form after Submit: eight code boxes; the right code leads to the confirmation."""

    def __init__(self, code="Ab3dE5fG", submitted=False):
        self.code, self.boxes, self.submits = code, [""] * 8, 1 if submitted else 0
        self.confirmed, self.error = False, ""

    async def evaluate(self, function, *args):
        if function is direct._PAGE_STATE:
            text = "Thank you for applying to Harborview!" if self.confirmed else "Apply for Data Engineer Submit application"
            return json.dumps({"url": "https://x/apply", "errors": [self.error] if self.error else [], "challenge": False,
                               "text": text})
        if function is direct._CODE_PROMPT:
            if self.confirmed or not self.submits:
                return json.dumps({"found": False})
            return json.dumps({"found": True, "boxes": 8, "length": 8, "error": self.error, "value": "".join(self.boxes),
                               "prompt": "A verification code was sent to j***@example.com. Enter the 8-character code."})
        if function is direct._CODE_BUTTON:
            return json.dumps({"found": True, "enabled": all(self.boxes)})
        if function is direct._BUTTON_STATE:
            return json.dumps({"found": True, "busy": False})
        if function is direct._SCROLL_TO:
            return "true"
        raise AssertionError(f"unexpected script {function[:40]!r}")

    async def get_elements_by_css_selector(self, selector):
        if selector.startswith('[data-bellamy-code="'):
            return [Box(self, int(selector.split('"')[1]))]
        return [SubmitButton(self)]


class Box:
    def __init__(self, page, index):
        self.page, self.index = page, index

    async def fill(self, value):
        self.page.boxes[self.index] = value


class SubmitButton:
    def __init__(self, page):
        self.page = page

    async def click(self):
        self.page.submits += 1
        if self.page.submits > 1:  # the code was sent along
            right = "".join(self.page.boxes) == self.page.code
            self.page.confirmed, self.page.error = right, "" if right else "Invalid security code"


async def _no_wait(_seconds):
    return None


class DeafCodePage(CodePage):
    """Greenhouse after the code is typed: the first Submit click goes unheard; a second press sends it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.js_presses = 0

    async def evaluate(self, function, *args):
        if function is direct._CLICK_JS:
            self.js_presses += 1
            self.confirmed = "".join(self.boxes) == self.code
            return "true"
        if function is direct._BUTTON_STATE:
            return json.dumps({"found": True, "busy": False})
        return await super().evaluate(function, *args)

    async def get_elements_by_css_selector(self, selector):
        if selector == '[data-bellamy-code-submit="1"]':
            return [_Deaf()]
        return await super().get_elements_by_css_selector(selector)


class _Deaf:
    async def click(self):
        return None  # the page never hears it


class StillPage:
    """A form whose Submit does nothing: no busy button, no change. `wakes`: the press that works."""

    def __init__(self, wakes: int | None = None, busy_then_quiet: bool = False):
        self.presses, self.wakes, self.busy_then_quiet, self.polls = 0, wakes, busy_then_quiet, 0

    @property
    def sent(self):
        return self.wakes is not None and self.presses >= self.wakes

    async def evaluate(self, function, *args):
        if function is direct._PAGE_STATE:
            text = "Thank you for applying!" if self.sent else "Apply for Data Engineer Submit application"
            return json.dumps({"url": "https://x/apply", "errors": [], "challenge": False, "text": text})
        if function is direct._CODE_PROMPT:
            return json.dumps({"found": False})
        if function is direct._BUTTON_STATE:
            self.polls += 1
            return json.dumps({"found": True, "busy": self.busy_then_quiet and self.polls == 2})
        if function is direct._SCROLL_TO:
            return "true"
        if function is direct._CLICK_JS:
            self.presses += 1
            return "true"
        raise AssertionError(f"unexpected script {function[:40]!r}")

    async def get_elements_by_css_selector(self, selector):
        return [PressButton(self)]


class PressButton:
    def __init__(self, page):
        self.page = page

    async def click(self):
        self.page.presses += 1


class FakeClock:
    """asyncio's clock, moved on by every sleep, so a 45-second wait runs instantly."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class SubmitReactionTests(unittest.TestCase):
    def run_clocked(self, page, wait=45.0):
        clock = FakeClock()
        loop = mock.MagicMock()
        loop.time = clock.time
        with mock.patch.object(direct.asyncio, "sleep", clock.sleep), \
                mock.patch.object(direct.asyncio, "get_running_loop", return_value=loop):
            return asyncio.run(direct.submit(page, "button[type=submit]", wait=wait))

    def test_a_submit_that_draws_no_reaction_is_pressed_once_more_then_reported_unsent(self):
        page = StillPage()
        result = self.run_clocked(page)
        self.assertEqual(page.presses, 2)
        self.assertFalse(result["submitted"])
        self.assertTrue(result["form_unchanged"])
        self.assertEqual(result["blockers"], [direct.UNCHANGED])

    def test_the_second_press_can_finish_the_application(self):
        page = StillPage(wakes=2)
        result = self.run_clocked(page)
        self.assertTrue(result["submitted"])
        self.assertEqual(page.presses, 2)

    def test_a_busy_button_means_the_form_was_sent_so_no_second_press(self):
        page = StillPage(busy_then_quiet=True)
        result = self.run_clocked(page)
        self.assertEqual(page.presses, 1)
        self.assertFalse(result.get("form_unchanged", False))
        self.assertEqual(result["blockers"], ["no confirmation seen after submit"])


class FalseCaptchaTests(unittest.TestCase):
    def make(self, challenge: bool):
        instance = worker.Worker()
        instance._ensure = mock.AsyncMock()
        instance.browser = mock.MagicMock()
        instance.browser.must_get_current_page = mock.AsyncMock(return_value="page")
        state = {"url": "https://x", "errors": [], "challenge": challenge, "text": ""}
        return instance, mock.patch.object(direct, "_js", mock.AsyncMock(return_value=state))

    def test_a_captcha_the_page_does_not_show_is_dropped(self):
        instance, js = self.make(challenge=False)
        read = worker.FormRead(is_application_form=True, blockers=["captcha", "Recaptcha requires verification."])
        instance.run = mock.AsyncMock(return_value=(read, {"cost": 0.01}, True))
        with js:
            reply = asyncio.run(instance.handle({"cmd": "read_form", "url": "https://x"}))
        self.assertEqual(reply["result"]["blockers"], [])

    def test_a_visible_challenge_stays_a_blocker(self):
        instance, js = self.make(challenge=True)
        report = worker.FillReport(blockers=["captcha: select all images with buses"])
        instance.run = mock.AsyncMock(return_value=(report, {"cost": 0.01}, True))
        with js:
            reply = asyncio.run(instance.handle({"cmd": "fill", "url": None, "plan": []}))
        self.assertEqual(reply["result"]["blockers"], ["captcha: select all images with buses"])

    def test_a_blocker_that_says_there_is_none_is_dropped(self):
        instance, js = self.make(challenge=True)  # even with reCAPTCHA's frame somewhere on the page
        report = worker.FillReport(blockers=["No errors or blockers observed; no active CAPTCHA challenge blocking the form.",
                                             "None", "Not logged in: the site requires an account"])
        instance.run = mock.AsyncMock(return_value=(report, {"cost": 0.01}, True))
        with js:
            reply = asyncio.run(instance.handle({"cmd": "fill", "url": None, "plan": []}))
        self.assertEqual(reply["result"]["blockers"], ["Not logged in: the site requires an account"])

    def test_other_blockers_are_kept(self):
        instance, js = self.make(challenge=False)
        read = worker.FormRead(blockers=["login_required", "captcha"])
        instance.run = mock.AsyncMock(return_value=(read, {"cost": 0.01}, True))
        with js:
            reply = asyncio.run(instance.handle({"cmd": "read_form", "url": "https://x"}))
        self.assertEqual(reply["result"]["blockers"], ["login_required"])


class StaleBrowserTests(unittest.TestCase):
    def test_only_an_orphaned_browser_on_bellamys_profile_is_stale(self):
        profile = "/home/x/bellamy/.browser-profile"
        flag = f"--user-data-dir={profile}"
        processes = [
            {"pid": 10, "ppid": 1, "cmdline": ["chrome", flag, "--headless"], "parent": "systemd"},  # orphan
            {"pid": 11, "ppid": 10, "cmdline": ["chrome", "--type=renderer", flag], "parent": "chrome"},  # its helper
            {"pid": 20, "ppid": 900, "cmdline": ["chrome", flag], "parent": "python"},  # a live worker's browser
            {"pid": 30, "ppid": 1, "cmdline": ["chrome", "--user-data-dir=/home/x/other"], "parent": "systemd"},
            {"pid": 40, "ppid": 1, "cmdline": ["sshd"], "parent": "systemd"},
        ]
        self.assertEqual(worker.stale_browsers(processes, profile), [10])


class CodeStepTests(unittest.TestCase):
    def run_fast(self, coroutine):
        with mock.patch.object(direct.asyncio, "sleep", _no_wait):
            return asyncio.run(coroutine)

    def test_code_length_from_the_prompt(self):
        self.assertEqual(direct.code_length("enter the 8-character code to confirm"), 8)
        self.assertEqual(direct.code_length("Type the 6 digit code we sent"), 6)
        self.assertEqual(direct.code_length("Enter the code"), 0)

    def test_submit_that_asks_for_an_emailed_code_leaves_the_form_open(self):
        page = CodePage()
        result = self.run_fast(direct.submit(page, "button[type=submit]"))
        self.assertFalse(result["submitted"])
        self.assertTrue(result["code_required"])
        self.assertEqual(result["code_length"], 8)
        self.assertEqual(result["blockers"], [direct.CODE_ASKED])
        self.assertIn("j***@example.com", result["code_prompt"])
        self.assertEqual(page.submits, 1)

    def test_enter_code_types_one_character_per_box_then_submits(self):
        page = CodePage(submitted=True)
        result = self.run_fast(direct.enter_code(page, "Ab3dE5fG"))
        self.assertEqual(page.boxes, list("Ab3dE5fG"))
        self.assertEqual(page.submits, 2)
        self.assertTrue(result["submitted"])
        self.assertFalse(result.get("code_required"))

    def test_a_code_submit_that_goes_unheard_is_pressed_again(self):
        page = DeafCodePage(submitted=True)
        clock = FakeClock()
        loop = mock.MagicMock()
        loop.time = clock.time
        with mock.patch.object(direct.asyncio, "sleep", clock.sleep), \
                mock.patch.object(direct.asyncio, "get_running_loop", return_value=loop):
            result = asyncio.run(direct.enter_code(page, "Ab3dE5fG"))
        self.assertTrue(result["submitted"])
        self.assertEqual(page.js_presses, 1)  # the first click went unheard; the second press sent it

    def test_a_wrong_code_comes_back_with_the_sites_message(self):
        page = CodePage(submitted=True)
        result = self.run_fast(direct.enter_code(page, "ZZZZZZZZ"))
        self.assertFalse(result["submitted"])
        self.assertTrue(result["code_required"])
        self.assertEqual(result["code_error"], "Invalid security code")

    def test_a_code_of_the_wrong_length_is_not_typed(self):
        page = CodePage(submitted=True)
        result = self.run_fast(direct.enter_code(page, "Ab3d"))
        self.assertIn("8 boxes", result["code_error"])
        self.assertEqual((page.boxes, page.submits), ([""] * 8, 1))

    def test_no_code_box_on_the_page(self):
        result = self.run_fast(direct.enter_code(CodePage(), "Ab3dE5fG"))
        self.assertEqual(result["blockers"], ["the code field is not on the page"])


if __name__ == "__main__":
    unittest.main()
