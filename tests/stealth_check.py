"""Zero-LLM stealth fingerprint verification.

Launches Chromium with Bellamy's stealth args (no LLM involved) and checks
the classic automation tells on bot.sannysoft.com (static fingerprint test page).
"""
import asyncio
import json
import subprocess
import time
import urllib.request
import os
import signal
import tempfile

CHROME = os.path.expanduser("~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
PROFILE = os.path.expanduser("~/bellamy/.browser-profile")

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run", "--no-default-browser-check", "--disable-infobars",
    "--password-store=basic", "--lang=en-US,en",
    "--window-size=1440,900",
    # Match BrowserProfile(chromium_sandbox=False) on Ubuntu 24.04/AppArmor.
    "--no-sandbox", "--disable-gpu-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--no-xshm", "--no-zygote",
]
AUTOMATION_TELLS = ["--enable-automation"]

EVAL_JS = r"""
(() => {
  const results = {
    webdriver: navigator.webdriver,
    userAgent: navigator.userAgent,
    languages: navigator.languages,
    plugins: navigator.plugins.length,
    chrome_obj: typeof window.chrome === "object",
    permissions_query: "permissions" in navigator,
    hardwareConcurrency: navigator.hardwareConcurrency,
    platform: navigator.platform,
  };
  return JSON.stringify(results);
})()
"""

async def main():
    # --remote-debugging-port with a throwaway user-data-dir (don't disturb bellamy's identity)
    tmp = tempfile.mkdtemp(prefix="bellamy-stealth-test-")
    port = 9333
    cmd = [CHROME, "--headless=new", f"--remote-debugging-port={port}",
           f"--user-data-dir={tmp}", f"--user-agent={UA}"] + STEALTH_ARGS
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        # wait for CDP, but fail with Chromium stderr instead of an opaque connection error
        cdp_ready = False
        for _ in range(40):
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                cdp_ready = True
                break
            except Exception:
                time.sleep(0.25)
        if not cdp_ready:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"Chromium did not expose CDP (exit={proc.poll()}):\n{stderr[-3000:]}")

        import websockets
        ws_url = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version").read())["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
            # open bot.sannysoft.com
            await ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                      "params": {"url": "https://bot.sannysoft.com/"}}))
            await ws.recv()
            await asyncio.sleep(6)  # let page load

            # find the page target
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read())
            page = next((t for t in targets if t["type"] == "page" and "sannysoft" in t.get("url", "")), None)
            if not page:
                print("❌ could not find page target")
                return

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                      "params": {"expression": EVAL_JS, "returnByValue": True}}))
            resp = json.loads(await ws.recv())
            fp = json.loads(resp["result"]["result"]["value"])

            # Also scrape the sannysoft table results
            await ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {
                "expression": r"""
                  (() => {
                    const rows = Array.from(document.querySelectorAll('tr'));
                    const out = [];
                    for (const r of rows) {
                      const cells = r.querySelectorAll('td');
                      if (cells.length >= 2) out.push(cells[0].innerText.trim() + ': ' + cells[1].innerText.trim());
                    }
                    return out.join('\n');
                  })()""",
                "returnByValue": True}}))
            resp2 = json.loads(await ws.recv())
            table = resp2["result"]["result"].get("value", "")

        print("═" * 55)
        print("FINGERPRINT (navigator):")
        print("═" * 55)
        checks = {
            "webdriver flag (must be false)": fp["webdriver"] is False,
            "UA has no 'Headless' marker": "headless" not in fp["userAgent"].lower(),
            "UA is real-Chrome string": "Chrome/151" in fp["userAgent"],
            "window.chrome object present": fp["chrome_obj"],
            "plugins > 0 (real browser)": fp["plugins"] >= 1,
        }
        for k, ok in checks.items():
            print(f"  {'✅' if ok else '❌'} {k}")
        print(f"     webdriver = {fp['webdriver']}")
        print(f"     UA = {fp['userAgent']}")
        print(f"     plugins = {fp['plugins']}, languages = {fp['languages']}")

        print("═" * 55)
        print("bot.sannysoft.com DIAGNOSTIC ROWS:")
        print("═" * 55)
        print("  (The site renders most pass/fail state as CSS color, so these rows")
        print("   are informational only; authoritative checks are shown above.)")
        if table:
            for line in table.split("\n"):
                if any(k in line for k in ("WebDriver Advanced", "Plugins is of type", "WebGL")):
                    print(f"  • {line}")
        else:
            print("  (page table not readable)")
    finally:
        proc.send_signal(signal.SIGTERM)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

asyncio.run(main())
