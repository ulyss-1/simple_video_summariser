"""uvicorn's own log lines must be JSON too (issue #58).

uvicorn applies its plain-text logging config, imports the app, logs
"Started server process", and only then runs the lifespan. A subprocess is
the only honest way to test import-time behaviour.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = textwrap.dedent(
    """
    import logging, logging.config
    from uvicorn.config import LOGGING_CONFIG
    logging.config.dictConfig(LOGGING_CONFIG)   # what uvicorn does first
    import services.api.main                    # then it imports the app
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    logging.getLogger("uvicorn.access").info("GET /healthz 200")
    """
)


def test_uvicorn_lines_are_json_once_the_app_is_imported() -> None:
    env = {
        "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db",
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
    }
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True,
        check=True, timeout=60, cwd=ROOT,
    )  # fmt: skip
    out = [ln for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in out]
    assert [p["event"] for p in parsed] == ["Started server process [1]"]
