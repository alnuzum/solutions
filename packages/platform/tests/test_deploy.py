"""A setting added in the repository reaches the container of the next deploy with no step on the server.

The chain: the job's environment (ci.yml) → ``ci-deploy.sh`` (the names it sends) → ``apps-deploy env`` (the
names it accepts, checked with bash itself) → ``.env`` → ``compose.app.yml`` (``env_file``) → the container.
"""

import re
import shutil
import subprocess  # nosec B404: bash with fixed arguments
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy"


def _sent_names() -> list[str]:
    script = (DEPLOY / "ci-deploy.sh").read_text()
    block = script[script.index("for key in ") : script.index("; do", script.index("for key in "))]
    return block.removeprefix("for key in ").replace("\\\n", " ").split()


def _accepted(names: list[str]) -> dict[str, bool]:
    """Each name through the very test ``apps-deploy env`` applies to a line (taken from the script)."""
    lines = (DEPLOY / "server" / "apps-deploy").read_text().splitlines()
    rules = "\n".join(line for line in lines if line.startswith(("ALLOWED=", "DENIED=", "SAFE_VALUE=")))
    used = next(line for line in lines if '"$key" =~ $ALLOWED' in line)
    test = re.search(r"\[\[ .*? \]\]", used)
    assert test, used
    check = (
        'for key in "$@"; do line="$key=1"; value=1; '
        f'if {test.group(0)}; then echo "$key 1"; else echo "$key 0"; fi; done'
    )
    bash = shutil.which("bash")
    assert bash
    out = subprocess.run(  # noqa: S603  # nosec B603
        [bash, "-c", f"{rules}\n{check}", "check", *names], capture_output=True, text=True, check=True
    ).stdout
    return {k: v == "1" for k, v in (line.split() for line in out.splitlines())}


def test_every_name_the_deploy_sends_is_accepted_by_the_server() -> None:
    names = _sent_names()
    assert {
        "VOICE_USD_PER_DAY",
        "VOICE_STT_MINUTES_PER_DAY",
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
    } <= set(names)
    assert all(_accepted(names).values()), [k for k, ok in _accepted(names).items() if not ok]


def test_a_setting_never_seen_before_needs_no_change_on_the_server() -> None:
    assert _accepted(["SOME_NEW_SETTING_2027"]) == {"SOME_NEW_SETTING_2027": True}


@pytest.mark.parametrize(
    "name",
    [
        "APP_ENV",
        "PUBLIC_URL",
        "IMAGE_TAG",
        "DATABASE_URL",
        "PATH",
        "LD_PRELOAD",
        "PYTHONPATH",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_HOST",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "OPENSSL_CONF",
        "HOME",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PYTHONSAFEPATH",
        "LD_LIBRARY_PATH",
        "GCONV_PATH",
        "GLIBC_TUNABLES",
        "MALLOC_ARENA_MAX",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "VIRTUAL_ENV",
        "BASH_ENV",
        "ENV",
        "HOSTALIASES",
        "RES_OPTIONS",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_LOG",
        "ANTHROPIC_CUSTOM_HEADERS",
        "lower",
        "A",
    ],
)
def test_names_that_change_how_things_run_are_refused(name: str) -> None:
    assert _accepted([name]) == {name: False}


def test_the_container_gets_the_whole_env_file() -> None:
    compose = yaml.safe_load((DEPLOY / "server" / "compose.app.yml").read_text())
    assert compose["services"]["platform"]["env_file"] == ["/srv/apps/${APP_ENV:?}/.env"]


def test_every_setting_of_the_deploy_jobs_is_sent() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    sent = set(_sent_names())
    for job in ("deploy-stand", "deploy-prod"):
        step = next(s for s in ci["jobs"][job]["steps"] if s.get("name") == "Build, ship, start")
        app = {k for k in step["env"] if not k.startswith("DEPLOY_")}
        assert app <= sent, sorted(app - sent)
        assert re.search(r"ci-deploy\.sh (stand|prod)", step["run"])


def test_the_access_log_drops_every_header_that_carries_a_secret() -> None:
    caddy = (DEPLOY / "server" / "Caddyfile").read_text()
    for header in ("Referer", "X-Telegram-Bot-Api-Secret-Token"):
        assert f"request>headers>{header} delete" in caddy
    assert 'request>uri regexp "\\?.*$" "?_"' in caddy


def test_the_caddyfile_is_replaced_in_place_and_reloaded() -> None:
    """Caddy mounts the file itself: a new inode would leave the running Caddy on the old rules."""
    boot = (DEPLOY / "server" / "bootstrap.sh").read_text()
    assert 'install -m 0644 "$HERE/Caddyfile"' not in boot
    assert 'cat "$HERE/Caddyfile" > /srv/apps/edge/Caddyfile' in boot
    assert "caddy reload --config /etc/caddy/Caddyfile" in boot


def test_the_paid_voice_job_does_not_run_on_a_push_of_documents() -> None:
    e2e = yaml.safe_load((ROOT / ".github" / "workflows" / "e2e.yml").read_text())
    push = e2e[True]["push"]  # YAML 1.1 reads «on» as true
    assert {"docs/**", "**/*.md"} <= set(push["paths-ignore"])
    assert "push" in e2e["jobs"]["voice-quality"]["if"]  # on a push of code it still runs
