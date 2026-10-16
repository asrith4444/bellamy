"""Stealth engine: fingerprint masking, persistent identity, human pacing.

Detection vectors covered:
  1. Automation flags        -> browser-use masks navigator.webdriver & disables
                                 blink AutomationControlled; we drop --enable-automation.
  2. Headless UA leak        -> force a real-Chrome user agent string.
  3. Fresh profile / no cookies -> persistent user_data_dir keeps sessions, cookies,
                                 localStorage between runs (looks like a returning human).
  4. Robot timing            -> jittered inter-step delays, occasional long "reading"
                                 pauses, non-zero action gaps.
"""

import asyncio
import random
from pathlib import Path

from browser_use.browser.profile import BrowserProfile

from .paths import BROWSER_PROFILE_DIR as PROFILE_DIR

# Real Chrome UA on Linux (no "Headless" marker — safe even in headless mode)
REAL_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

STEALTH_EXTRA_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--password-store=basic",
    "--lang=en-US,en",
]

# Chrome switches that scream "automation" — strip them from defaults.
AUTOMATION_TELLS = [
    "--enable-automation",
    "--disable-features=AutomationControlled",  # replaced by our own merged args
]


def build_stealth_profile(headless: bool = False, keep_alive: bool = True) -> BrowserProfile:
    """Return a hardened BrowserProfile that mimics a real returning user."""
    return BrowserProfile(
        headless=headless,
        user_data_dir=str(PROFILE_DIR),      # persistent identity & sessions
        user_agent=REAL_CHROME_UA,            # mask headless UA
        args=list(STEALTH_EXTRA_ARGS),
        ignore_default_args=list(AUTOMATION_TELLS),
        chromium_sandbox=False,               # Ubuntu 24.04 AppArmor compat
        wait_between_actions=random.uniform(0.6, 1.4),  # gap between individual actions
        minimum_wait_page_load_time=0.5,
        wait_for_network_idle_page_load_time=random.uniform(0.8, 1.6),
        window_size={"width": 1440, "height": 900},
        keep_alive=keep_alive,
        highlight_elements=False,             # DOM highlights are invisible anyway, keep clean
    )


async def human_step_callback(_state, _output, _step: int):
    """browser-use step callback: purposeful, human-like pauses between steps.

    Gaussian around 3.5s, hard-clipped; 12% chance of a long "reading" pause.
    Sites like LinkedIn flag bursts of sub-second actions as bots.
    """
    delay = max(1.2, min(8.0, random.gauss(3.5, 1.6)))
    if random.random() < 0.12:          # occasionally "read" the page like a human
        delay = random.uniform(9.0, 18.0)
    await asyncio.sleep(delay)
