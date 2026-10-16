#!/usr/bin/env python3
"""Bellamy CLI — the Hyena. Stealth autonomous browser agent.

Usage:
  bellamy "task..."                 run an autonomous task
  bellamy jobs "python dev" --location remote --sites linkedin,indeed
  bellamy extract <job-url>         pull a full job description
  bellamy apply <job-url>           apply using ~/bellamy/profile.json
  bellamy serve                     run as MCP server (for pi/opencode)
  bellamy worker                    JSON-lines read/fill/submit session (used by Mark)
  bellamy --last / --logs           view results
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def build_parser():
    p = argparse.ArgumentParser(prog="bellamy", description="🐺 Bellamy — stealth autonomous browser agent")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="run an autonomous task")
    run.add_argument("task")
    run.add_argument("-m", "--model", default=None)
    run.add_argument("-s", "--steps", type=int, default=60)
    run.add_argument("--vision", choices=["auto", "always", "never"], default="auto")
    run.add_argument("--headed", action="store_true")
    run.add_argument("--headless", action="store_true")
    run.add_argument("--no-mcp", action="store_true", help="skip MCP servers")
    run.add_argument("-o", "--output", default=None)

    jobs = sub.add_parser("jobs", help="search job boards")
    jobs.add_argument("query")
    jobs.add_argument("--location", default="")
    jobs.add_argument("--sites", default="linkedin,indeed")
    jobs.add_argument("-s", "--steps", type=int, default=40)
    jobs.add_argument("-m", "--model", default=None, help=argparse.SUPPRESS)
    jobs.add_argument("--headed", action="store_true")
    jobs.add_argument("--headless", action="store_true")
    jobs.add_argument("--no-mcp", action="store_true", help="skip MCP credential servers for read-only search")

    ext = sub.add_parser("extract", help="extract a job description from URL")
    ext.add_argument("url")
    ext.add_argument("-m", "--model", default=None, help=argparse.SUPPRESS)
    ext.add_argument("--headed", action="store_true")
    ext.add_argument("--headless", action="store_true")

    app = sub.add_parser("apply", help="apply to a job at URL")
    app.add_argument("url")
    app.add_argument("--notes", default="")
    app.add_argument("-m", "--model", default=None, help=argparse.SUPPRESS)

    sub.add_parser("serve", help="run Bellamy as an MCP stdio server")
    sub.add_parser("worker", help="JSON-lines form session for other programs (read, fill, submit)", add_help=False)
    doctor = sub.add_parser("doctor", help="zero-LLM installation and stealth diagnostics")
    doctor.add_argument("--stealth", action="store_true", help="also run the live fingerprint check")

    p.add_argument("--last", action="store_true")
    p.add_argument("--logs", action="store_true")
    return p


def show_last():
    from bellamy.agent import LOGS_DIR
    rf = LOGS_DIR / "latest" / "result.md"
    print(rf.read_text(encoding="utf-8") if rf.exists() else "No runs yet.")


def show_logs():
    from bellamy.agent import LOGS_DIR
    if not LOGS_DIR.exists():
        print("No logs yet.")
        return
    runs = sorted((d for d in LOGS_DIR.iterdir() if d.is_dir() and d.name != "latest"), reverse=True)
    for d in runs[:15]:
        meta = {}
        mf = d / "metadata.json"
        if mf.exists():
            meta = json.loads(mf.read_text())
        status = "✅" if meta.get("is_successful") else "⚠️"
        print(f"{status} {d.name} | {meta.get('steps', '?')} steps | {meta.get('task', '')[:60]}")


def doctor(run_stealth: bool = False):
    """Validate Bellamy without making any OpenRouter/model calls."""
    from bellamy.paths import BELLAMY_HOME as root
    chrome = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
    checks = [
        ("Bellamy CLI in PATH", shutil.which("bellamy") is not None),
        ("Playwright Chromium installed", chrome.exists()),
        ("OpenRouter key configured", bool(os.getenv("OPENROUTER_API_KEY") or (root / ".env").exists())),
        ("Persistent browser profile", (root / ".browser-profile").exists()),
        ("Job profile completed", False),
        ("Resume file exists", False),
        ("pi extension installed", (Path.home() / ".pi/agent/extensions/bellamy.ts").exists()),
        ("opencode MCP config installed", (Path.home() / ".config/opencode/opencode.jsonc").exists()),
        ("1Password CLI (`op`)", shutil.which("op") is not None),
    ]
    try:
        profile = json.loads((root / "profile.json").read_text())
        checks[4] = (checks[4][0], bool(profile.get("full_name") and profile.get("email")))
        resume = Path(profile.get("resume_path", "")).expanduser()
        checks[5] = (checks[5][0], resume.is_file())
    except Exception:
        pass

    print("🐺 Bellamy doctor (zero LLM/API calls)\n")
    for label, ok in checks:
        optional = label.startswith("1Password")
        marker = "✅" if ok else ("ℹ️ " if optional else "❌")
        print(f"{marker} {label}")
    if not shutil.which("op"):
        print("   1Password integration remains disabled until `op` is installed and signed in.")

    try:
        cfg = json.loads((root / "mcp.json").read_text())
        onep = cfg.get("mcpServers", {}).get("1password", {})
        print(f"{'ℹ️ ' if onep.get('disabled') else '✅'} 1Password MCP: {'disabled (safe default)' if onep.get('disabled') else 'enabled'}")
    except Exception as e:
        print(f"❌ MCP config invalid: {e}")

    if run_stealth:
        print("\nRunning live Chromium fingerprint check (still zero LLM/API calls)…\n", flush=True)
        test = root / "tests/stealth_check.py"
        python = root / ".venv/bin/python"
        subprocess.run([str(python), str(test)], check=False)


async def main_async(args):
    from bellamy.agent import run_task
    from dotenv import load_dotenv
    import os
    from bellamy.paths import ENV_PATH
    load_dotenv(ENV_PATH)

    headless = None
    if getattr(args, "headed", False):
        headless = False
    elif getattr(args, "headless", False):
        headless = True

    if args.cmd == "run":
        task = args.task
        await run_task(
            task, model=args.model, headless=headless, vision=args.vision,
            max_steps=args.steps, output_path=args.output,
            use_mcp=not args.no_mcp,
        )
    elif args.cmd == "jobs":
        from bellamy.jobs import build_job_search_task
        task = build_job_search_task(
            args.query,
            location=args.location,
            sites=args.sites,
        )
        await run_task(
            task,
            model=args.model,
            headless=headless,
            max_steps=args.steps,
            use_mcp=not args.no_mcp,
        )
    elif args.cmd == "extract":
        task = (
            f"Open this job posting and extract the complete job description: {args.url}. "
            "Save it (title, company, location, salary if present, requirements, full text) "
            "to a markdown file in the jobs folder, then return a summary."
        )
        await run_task(task, model=args.model, headless=headless, max_steps=15)
    elif args.cmd == "apply":
        task = (
            f"Apply to this job posting: {args.url}. Use my saved profile for every field. "
            "Log in via 1Password MCP tools if authentication is required. Verify every field "
            "before submitting, confirm submission, and report the outcome."
        )
        if args.notes:
            task += f" Include these tailored notes where a cover letter is offered: {args.notes}"
        await run_task(task, model=args.model, headless=False, max_steps=80)
    elif args.cmd == "serve":
        from bellamy.server import mcp as bellamy_mcp
        await bellamy_mcp.run_stdio_async()
        return


def main():
    if sys.argv[1:2] == ["worker"]:
        from bellamy.worker import main as worker_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        worker_main()
        return
    parser = build_parser()

    # Ergonomic shortcut: `bellamy "browse task"` becomes
    # `bellamy run "browse task"`; explicit subcommands remain unchanged.
    argv = sys.argv[1:]
    known_commands = {"run", "jobs", "extract", "apply", "serve", "doctor"}
    if argv and not argv[0].startswith("-") and argv[0] not in known_commands:
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    if args.last:
        show_last()
        return
    if args.logs:
        show_logs()
        return

    if args.cmd is None:
        parser.print_help()
        return
    if args.cmd == "doctor":
        doctor(args.stealth)
        return

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
