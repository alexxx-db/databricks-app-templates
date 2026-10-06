"""Smoke tests for the classic Python templates (see CLASSIC_TEMPLATES).

For each template:
  1. requirements.txt resolves on the Databricks Apps Python version.
  2. The app.yaml command boots with placeholder credentials and serves HTTP 200 on /
     (skipped for templates that need a live workspace to start, see NEEDS_WORKSPACE_TO_BOOT).
  3. databricks.yml validates against the bundle JSON schema (skipped without the CLI).

No workspace is contacted: resource env vars get dummy values, so this catches
dependency breakage and startup crashes, not wrong queries.

Run from this directory:  uv run pytest -v -n 8
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from functools import cache
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / ".scripts"))
from templates import CLASSIC_TEMPLATES  # noqa: E402

PYTHON_VERSION = "3.11"  # Databricks Apps runtime
BOOT_TIMEOUT_S = 180  # first run includes the dependency install

# Dummy values for every env var a classic template reads at startup.
DUMMY_ENV = {
    "DATABRICKS_HOST": "https://example.cloud.databricks.com",
    "DATABRICKS_TOKEN": "dummy-token",
    "DATABRICKS_WAREHOUSE_ID": "dummy-warehouse",
    "SERVING_ENDPOINT": "dummy-endpoint",
    "PGENDPOINT": "projects/dummy/branches/dummy/endpoints/dummy",
    "PGHOST": "localhost",
    "PGPORT": "5432",
    "PGDATABASE": "dummy",
    "PGUSER": "dummy",
    "PGAPPNAME": "dummy",
    "DATABRICKS_JOB_ID": "1",
    "DATABRICKS_VOLUME_PATH": "/Volumes/dummy/dummy/dummy",
    "VECTOR_SEARCH_INDEX": "dummy.dummy.dummy",
    "STREAMLIT_SERVER_HEADLESS": "true",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
}

# These call a Databricks API at import time (endpoint lookup, SQL query, Lakebase
# credential), so they block on the dummy host instead of serving. Boot is skipped;
# resolve + schema checks still run.
# ponytail: boot these against a real workspace when a --profile option is worth adding.
NEEDS_WORKSPACE_TO_BOOT = {
    "dash-chatbot-app",
    "dash-data-app",
    "dash-database-app",
    "dash-postgres-app",
    "flask-database-app",
    "flask-postgres-app",
    "gradio-chatbot-app",
    "gradio-data-app",
    "shiny-chatbot-app",
}

# The React frontend is only built on deploy (npm run build), so / is 404 locally.
HEALTH_PATH = {"nodejs-fastapi-hello-world-app": "/api/hello"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _app_command(template_dir: Path, port: int) -> list[str]:
    cmd = list(yaml.safe_load((template_dir / "app.yaml").read_text())["command"])
    # Frameworks that only take the port as a flag. The rest read it from env (see _app_env).
    if cmd[0] in ("shiny", "uvicorn"):
        if "--port" in cmd:
            cmd[cmd.index("--port") + 1] = str(port)
        else:
            cmd += ["--port", str(port)]
    return cmd


def _app_env(template_dir: Path, port: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}
    env.update(DUMMY_ENV)
    for item in yaml.safe_load((template_dir / "app.yaml").read_text()).get("env", []):
        if "value" in item:
            env[item["name"]] = str(item["value"])
    port_s = str(port)
    env.update(
        DATABRICKS_APP_PORT=port_s,
        PORT=port_s,  # dash
        STREAMLIT_SERVER_PORT=port_s,
        GRADIO_SERVER_PORT=port_s,
        FLASK_RUN_PORT=port_s,
    )
    return env


def _get(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return None


@pytest.mark.parametrize("template", CLASSIC_TEMPLATES)
def test_requirements_resolve(template):
    result = subprocess.run(
        ["uv", "pip", "compile", "-q", "--python-version", PYTHON_VERSION, "requirements.txt"],
        cwd=REPO_ROOT / template,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("template", CLASSIC_TEMPLATES)
def test_app_boots(template, tmp_path):
    if template in NEEDS_WORKSPACE_TO_BOOT:
        pytest.skip("calls Databricks APIs at import time; needs a live workspace")
    template_dir = REPO_ROOT / template
    path = HEALTH_PATH.get(template, "/")
    port = _free_port()
    log = tmp_path / "app.log"
    cmd = [
        "uv", "run", "--no-project", "--isolated", "--python", PYTHON_VERSION,
        "--with-requirements", "requirements.txt", "--",
        *_app_command(template_dir, port),
    ]
    with log.open("w") as out:
        proc = subprocess.Popen(
            cmd,
            cwd=template_dir,
            env=_app_env(template_dir, port),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # so dev-mode reloaders die with the group
        )
    try:
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        status = None
        while time.monotonic() < deadline and proc.poll() is None:
            status = _get(f"http://127.0.0.1:{port}{path}")
            if status is not None:
                break
            time.sleep(1)
        assert status == 200, (
            f"expected HTTP 200 on {path}, got {status} (exit={proc.poll()})\n{log.read_text()[-3000:]}"
        )
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(proc.pid, signal.SIGKILL)


@cache
def _bundle_schema() -> dict:
    raw = subprocess.check_output(["databricks", "bundle", "schema"], text=True)
    # The schema's patterns are Go regex; Python `re` has no \p{..} classes.
    raw = (
        raw.replace(r"[\\p{L}\\p{N}]", r"[^\\W_]")  # letter or digit
        .replace(r"\\p{L}", r"[^\\W\\d_]")  # letter
        .replace(r"\\p{N}", r"\\d")
    )
    return json.loads(raw)


@pytest.mark.skipif(shutil.which("databricks") is None, reason="databricks CLI not installed")
@pytest.mark.parametrize("template", CLASSIC_TEMPLATES)
def test_bundle_schema(template):
    import jsonschema

    bundle = yaml.safe_load((REPO_ROOT / template / "databricks.yml").read_text())
    jsonschema.validate(bundle, _bundle_schema())
