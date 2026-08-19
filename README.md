# 🐺 Bellamy — Stealth Autonomous Browser Agent

> *"The Hyena never lets go."* — Bellamy, One Piece

Bellamy is a stealthy, human-paced, autonomous browser agent powered by
**browser-use + Playwright Chromium + OpenRouter (GPT-5.6 Luna)** with **MCP support**
(1Password, etc.), built for **job hunting** — searching boards, extracting job
descriptions, and submitting applications.

---

## Quick Start

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

---

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
