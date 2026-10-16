"""Where Bellamy keeps its config and data.

BELLAMY_HOME (default ~/bellamy) holds .env, profile.json, mcp.json, the persistent
browser profile, saved job descriptions and run logs. The code can live anywhere.
"""

import os
from pathlib import Path

BELLAMY_HOME = Path(os.getenv("BELLAMY_HOME", "~/bellamy")).expanduser()
ENV_PATH = BELLAMY_HOME / ".env"
LOGS_DIR = BELLAMY_HOME / "logs"
JOBS_DIR = BELLAMY_HOME / "jobs"
PROFILE_PATH = BELLAMY_HOME / "profile.json"
MCP_CONFIG_PATH = BELLAMY_HOME / "mcp.json"
BROWSER_PROFILE_DIR = BELLAMY_HOME / ".browser-profile"
