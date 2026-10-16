"""Bellamy MCP server — lets other agents (pi, opencode, Claude Code…) drive Bellamy.

Run with:  bellamy serve
Then register in opencode.json / client MCP config:

  "bellamy": {"command": "bellamy", "args": ["serve"]}

Tools exposed:
  bellamy_task      — run any autonomous browsing task
  bellamy_job_search — search job boards & return listings
  bellamy_extract_job — fetch full job description from a URL
  bellamy_apply_job   — apply to a job URL using BELLAMY_HOME/profile.json
  bellamy_last        — return the last run's result
"""

import asyncio
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .agent import run_task, LOGS_DIR
from .jobs import build_job_search_task

mcp = FastMCP("bellamy")


@mcp.tool()
async def bellamy_task(task: str, max_steps: int = 60, vision: str = "auto", headless: bool = True) -> str:
    """Run an autonomous browser task (stealth, human-paced, MCP-aware).

    Args:
        task: natural-language instruction, e.g. "go to X and extract Y"
        max_steps: step budget (default 60)
        vision: 'auto' (recommended), 'never', or 'always'
        headless: run without a visible window (default true)
    """
    out = await run_task(task, max_steps=max_steps, vision=vision, headless=headless, quiet=True)
    return f"[{'SUCCESS' if out['success'] else 'INCOMPLETE'} in {out['steps']} steps]\n{out['result']}\n(log: {out['log_dir']})"


@mcp.tool()
async def bellamy_job_search(query: str, location: str = "", sites: str = "linkedin,indeed", max_steps: int = 40) -> str:
    """Search job boards for openings matching a role.

    Args:
        query: job title or keywords, e.g. "senior python developer"
        location: optional location filter, e.g. "remote" or "Berlin"
        sites: comma-separated list such as linkedin, indeed, glassdoor, dice
    """
    task = build_job_search_task(query, location=location, sites=sites)
    out = await run_task(task, max_steps=max_steps, use_mcp=False, quiet=True)
    return f"[{'SUCCESS' if out['success'] else 'INCOMPLETE'} in {out['steps']} steps]\n{out['result']}"


@mcp.tool()
async def bellamy_extract_job(url: str, save: bool = True) -> str:
    """Fetch the full job description from a posting URL.

    Args:
        url: direct link to the job posting
        save: also write it to BELLAMY_HOME/jobs/<company>_<role>.md
    """
    save_clause = (
        " Then save the full description (title, company, location, salary if present, "
        "requirements, and the complete posting text) to a markdown file in the jobs folder."
        if save else ""
    )
    task = f"Open this job posting and extract the complete job description: {url}.{save_clause}"
    out = await run_task(task, max_steps=15, use_mcp=False, quiet=True)
    return f"[{'SUCCESS' if out['success'] else 'INCOMPLETE'} in {out['steps']} steps]\n{out['result']}"


@mcp.tool()
async def bellamy_apply_job(url: str, notes: str = "") -> str:
    """Apply to a job posting using the profile in BELLAMY_HOME/profile.json.

    Uses 1Password MCP for any login/credential needs; never invents personal data.
    Args:
        url: direct link to the job posting / application form
        notes: optional cover-letter notes or tailored points to include
    """
    task = (
        f"Apply to this job posting: {url}. Use my saved profile for every field. "
        "Log in via 1Password MCP tools if authentication is required. "
        "Verify every field before submitting, confirm the submission, and report "
        "the outcome plus confirmation details."
    )
    if notes:
        task += f" Include these tailored notes where a cover letter/message is offered: {notes}"
    out = await run_task(task, max_steps=80, headless=False, quiet=True)
    return f"[{'SUCCESS' if out['success'] else 'INCOMPLETE'} in {out['steps']} steps]\n{out['result']}"


@mcp.tool()
async def bellamy_last() -> str:
    """Return the result summary of Bellamy's most recent run."""
    latest = LOGS_DIR / "latest"
    rf = latest / "result.md"
    if rf.exists():
        return rf.read_text(encoding="utf-8")[:8000]
    return "No previous Bellamy runs found."


def main():
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
