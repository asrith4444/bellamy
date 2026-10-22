# 🐺 Bellamy — Stealth Autonomous Browser Agent

> *"The Hyena never lets go."* — Bellamy, One Piece

Bellamy is a stealthy, human-paced, autonomous browser agent powered by
**browser-use + Playwright Chromium + OpenRouter** with **MCP support** (1Password,
etc.), built for **job hunting** — searching boards, extracting job descriptions,
and submitting applications. It uses its own `.venv` environment. Config and data live in `BELLAMY_HOME`
(default `~/bellamy`), so the code can be checked out anywhere.

---

## Quick Start

Install Bellamy's pinned dependencies into its own environment:

```bash
cd ~/bellamy
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Then run:

```bash
bellamy "Go to https://news.ycombinator.com and summarize the top 3 stories"
bellamy jobs "senior python developer" --location remote --sites linkedin,indeed
bellamy extract https://www.linkedin.com/jobs/view/1234567890
bellamy apply  https://www.linkedin.com/jobs/view/1234567890 --notes "excited about the infra focus"
bellamy --last          # view last run result
bellamy --logs          # list recent runs
bellamy serve           # run as MCP server (for opencode / other MCP clients)
bellamy doctor          # validate setup without any LLM/API calls
bellamy doctor --stealth # live fingerprint check; still zero LLM calls
```

Flags: `-s/--steps`, `--vision auto|always|never`, `--headed/--headless`, `--no-mcp`, `-o out.md`.

---

## 🥷 Stealth Engine

| Vector | Countermeasure |
| :--- | :--- |
| `navigator.webdriver`, automation flags | blink `AutomationControlled` disabled, `--enable-automation` stripped, fingerprint masked by browser-use |
| Headless UA leak | real Chrome user-agent forced (even headless) |
| Fresh-profile suspicion | **persistent identity** — `~/bellamy/.browser-profile` keeps cookies, sessions, localStorage between runs (LinkedIn remembers you) |
| Robot timing | jittered human delays between every step (gaussian ~3.5s, occasional 9–18s "reading" pauses) + randomized action gaps & page-load waits |
| Behavioral patterns | prompt-level stealth protocol: no burst navigation, no rapid-fire searches, stop on CAPTCHA/rate-limit |

**Tip:** for sensitive sites (LinkedIn applications), run `--headed` on a desktop session —
a visible real window is the strongest stealth signal, and sessions persist afterwards.

---

## 💼 Job Hunting

1. Fill `~/bellamy/profile.json` (name, email, resume path, links…). Bellamy uses **only** these values — never invents data.
2. Credentials: install and sign in to the official 1Password CLI (`op`), then enable
   `@jrejaud/op-mcp` in `~/bellamy/mcp.json` by removing `"disabled": true`.
   When MCP is active, Bellamy disables raw conversation transcript logging so
   tool-returned secrets are not persisted. Never put a service-account token directly
   in `mcp.json`; inherit it from your shell environment if one is required.
3. Extracted job descriptions are saved to `~/bellamy/jobs/<company>_<role>.md`.
4. Boolean `OR` job searches are planned inside Bellamy as independent sequential searches, then merged and deduplicated. Dice receives separate current `/jobs?q=...` URLs because its search route does not reliably accept a literal combined `OR` query.
5. Bellamy never guesses legacy API hosts from model memory. It uses the live public UI or endpoints discovered from the current site. The retired `service.dice.com` host is explicitly prohibited.

Bellamy intentionally uses one browser instance/profile for this fan-out. Parallel instances sharing the profile would conflict, while separate identities from one IP increase request rate and bot-detection risk without materially improving a two-query search.

Every browser-use step is observed and decided by the model from the current live DOM/screenshot. A runtime circuit breaker complements that reasoning: one retry is allowed for a hard endpoint error, the next repeated retry is replaced with a fresh decision cycle, and a second ignored warning ends unsuccessfully. CAPTCHA and rate-limit pages stop immediately. Genuine pivots to another endpoint/query/site remain allowed.

---

## 🧾 Worker mode (used by Mark)

`bellamy worker` (or `python -m bellamy.worker`) keeps one browser open and reads JSON
commands from stdin, one per line, answering one JSON line each on stdout:

| Command | Does | Returns |
| :--- | :--- | :--- |
| `read_form` | opens the posting, opens its form, lists every field | `FormRead` |
| `fill` | fills the form with exact `label => value` pairs, uploads files; **never submits** | `FillReport` |
| `submit` | clicks the final submit once and reads the confirmation | `SubmitResult` |
| `screenshot` | saves a full-page screenshot | path |
| `close` | ends the session | |

In `read_form` and `fill`, a deterministic submit guard blocks clicks on final submit
buttons, Enter key presses and scripted form submits, whatever the model decides. The
caller checks the fill report and only then sends `submit`. Every reply carries the run's
token usage and cost.

Forms are big pages, so the worker asks the model for brief reasoning and allows 120 seconds
per model call (browser-use's default is 75). Change them in `.env` with
`BELLAMY_REASONING_EFFORT` (`low` by default; empty for the model's own default) and
`BELLAMY_LLM_TIMEOUT` (seconds).

## 🔌 MCP

### Bellamy as an MCP server (for other agents)

`bellamy serve` exposes:

| Tool | Purpose |
| :--- | :--- |
| `bellamy_task` | run any autonomous browsing task |
| `bellamy_job_search` | search job boards, return listings |
| `bellamy_extract_job` | fetch full JD from a URL |
| `bellamy_apply_job` | apply to a job using profile + 1Password |
| `bellamy_last` | last run's result |

**opencode** (`~/.config/opencode/opencode.jsonc` — already configured and connection-tested):
```json
{ "mcp": { "bellamy": { "type": "local", "command": ["bellamy", "serve"], "enabled": true } } }
```

**pi** intentionally has no built-in MCP client. Bellamy is integrated through a native
pi custom-tool extension instead: `~/.pi/agent/extensions/bellamy.ts`. It is already
enabled and provides the same five tools after restarting pi.

**Any other MCP client** (Claude Code, Cursor, etc.):
```json
{ "mcpServers": { "bellamy": { "command": "bellamy", "args": ["serve"] } } }
```

### Bellamy as an MCP *client*

Any server in `~/bellamy/mcp.json` (Claude Desktop format) is auto-connected at task
start, and its tools are injected as browser-use actions the agent can call mid-browsing.

---

## 📂 Layout

```
~/bellamy/
├── main.py              CLI
├── bellamy/
│   ├── agent.py         core runner (persona, logging, MCP wiring)
│   ├── stealth.py       stealth profile + human pacing engine
│   ├── jobs.py          job-hunt protocol + profile injection
│   ├── mcp_manager.py   MCP client bridge → agent actions
│   └── server.py        MCP server for other agents
├── requirements.txt     pinned direct Python dependencies
├── .venv/               Bellamy's independent Python environment
├── profile.json         your job-seeker identity
├── mcp.json             MCP servers (1Password)
├── .browser-profile/    persistent stealth browser identity
├── jobs/                extracted job descriptions
└── logs/                result.md + metadata.json; conversation.json only when MCP is inactive
```

## Detection caveat

No automation framework can guarantee invisibility. Sites can combine browser and
behavioral fingerprints with account and IP reputation. Bellamy removes obvious
signals, uses conservative human pacing, and stops on CAPTCHA/rate-limit warnings,
but use reasonable rates and comply with each site's terms.

## ⚙️ Config (`~/bellamy/.env`)

```env
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEFAULT_MODEL=openai/gpt-5.6-luna
VISION_MODE=auto
```
