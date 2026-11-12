"""CLI (§19) against the real API app via a TestClient transport (CLI never touches the DB)."""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import cli.ads as ads
from tests.api.conftest import BASE, PASSWORD, World

pytestmark = pytest.mark.integration


class Cli:
    def __init__(self, token_file: Path) -> None:
        self.runner = CliRunner()
        self.token_file = token_file

    def __call__(self, *args: str, code: int = 0, input: str | None = None) -> str:
        base = ["--api-url", f"{BASE}/api/v1", "--token-file", str(self.token_file)]
        r = self.runner.invoke(ads.app, [*base, *args], input=input)
        out = r.output + (r.stderr if r.stderr_bytes is not None else "")
        assert r.exit_code == code, f"{args}: exit {r.exit_code}\n{out}\n{r.exception!r}"
        return out


@pytest.fixture
def cli(app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cli]:
    clients: list[TestClient] = []

    def factory(base_url: str) -> httpx.Client:
        c = TestClient(app, base_url=base_url)
        clients.append(c)
        return c

    monkeypatch.setattr(ads, "http_factory", factory)
    yield Cli(tmp_path / "cfg" / "credentials.json")
    for c in clients:
        c.close()


def _login(cli: Cli, role: str | None = None) -> None:
    extra = ["--role", role] if role else []
    out = cli("login", "--username", "admin", "--password", PASSWORD, *extra)
    assert "logged in as admin" in out


def test_login_stores_token_0600_and_logout_revokes(cli: Cli) -> None:
    cli("whoami", code=ads.EXIT_AUTH)  # not logged in yet
    cli("login", "--username", "admin", "--password", "wrong", code=ads.EXIT_AUTH)
    _login(cli)
    mode = stat.S_IMODE(cli.token_file.stat().st_mode)
    assert mode == 0o600
    data = json.loads(cli.token_file.read_text())
    assert data["api_url"] == f"{BASE}/api/v1" and data["role"] == "admin"
    assert "admin (admin, cli)" in cli("whoami")
    token = data["token"]
    assert "logged out" in cli("logout")
    assert not cli.token_file.exists()
    # the server-side session is revoked, not just the local file
    cli.token_file.parent.mkdir(exist_ok=True)
    cli.token_file.write_text(json.dumps({"api_url": f"{BASE}/api/v1", "token": token}))
    cli("whoami", code=ads.EXIT_AUTH)


def test_login_prompts_for_password(cli: Cli) -> None:
    out = cli("login", "--username", "admin", input=f"{PASSWORD}\n")
    assert "logged in" in out


def test_status(cli: Cli, world: World) -> None:
    _login(cli)
    out = cli("status")
    assert "mode     read_only (max suggest)" in out
    assert "40.00 / 12000" in out and "1590.00 / 550000" in out
    assert "approvals 3 open" in out and "1 warn" in out
    assert "hourly_pull" in out
    raw = json.loads(_json(cli, "status"))
    assert raw["overview"]["mode"] == "read_only"


def _json(cli: Cli, *args: str) -> str:
    base = ["--api-url", f"{BASE}/api/v1", "--token-file", str(cli.token_file), "--json"]
    r = cli.runner.invoke(ads.app, [*base, *args])
    assert r.exit_code == 0, r.output
    return r.output


def test_proposals_approve_reject(cli: Cli, world: World) -> None:
    _login(cli, "approver")
    out = cli("proposals")
    assert str(world.proposal_waiting) in out and "awaiting_approval" in out
    assert "approved" in cli("approve", str(world.proposal_waiting), "--comment", "ok")
    out = cli("approve", str(world.proposal_waiting2), str(world.proposal_expired), code=1)
    assert f"proposal {world.proposal_waiting2}: approved" in out
    assert f"proposal {world.proposal_expired}: FAILED" in out
    cli("reject", str(world.proposal_pending), code=2)  # --reason is required
    out = cli("reject", str(world.proposal_pending), "--reason", "no", code=1)
    assert "not awaiting_approval" in out
    all_rows = json.loads(_json(cli, "proposals", "--all"))
    assert {r["status"] for r in all_rows} >= {"approved", "pending"}


def test_write_path_commands(cli: Cli, world: World) -> None:
    _login(cli)
    out = cli("dry-run", str(world.proposal_waiting))
    assert "guardrail block mode_read_only" in out and "write gate: CLOSED" in out
    out = cli("rollback", str(world.change), "--yes")
    assert "queued rollback as write request #" in out
    req_id = out.split("#")[1].split()[0]
    assert "rollback: queued" in cli("write-request", req_id)
    cli("rollback", str(world.change), "--yes", code=1)  # already queued (409)
    cli("rollback", str(world.change), input="n\n", code=1)  # aborted at the prompt
    cli("pause-all", code=2)
    assert "queued pause_all" in cli("pause-all", "--platform", "meta", "--confirm")
    assert "queued restore" in cli("restore", str(world.emergency))
    assert "execution off" in cli("execution")
    assert "execution ON" in cli("execution", "on", "--reason", "test")
    assert "queued hourly_pull as request #" in cli("run", "hourly_pull")
    assert "queued emergency_restore" in cli("run", "emergency_restore")
    cli("run", "nope", code=1)


def test_control_commands(cli: Cli, world: World) -> None:
    _login(cli)
    assert f"campaign {world.meta_campaign} managed=True" in cli(
        "manage", str(world.meta_campaign), "on", "--reason", "opt in"
    )
    cli("manage", str(world.meta_campaign), "maybe", code=2)
    assert "mode suggest" in cli("mode", "suggest")
    out = cli("mode", "auto", code=ads.EXIT_AUTH)
    assert "HARD_MAX_MODE" in out
    assert "kill switch ON" in cli("kill-switch", "on")
    assert "kill switch off" in cli("kill-switch", "off")


def test_viewer_token_is_read_only(cli: Cli, world: World) -> None:
    _login(cli, "viewer")
    cli("status")
    out = cli("approve", str(world.proposal_waiting), code=ads.EXIT_AUTH)
    assert "forbidden" in out
    cli("kill-switch", "on", code=ads.EXIT_AUTH)


def test_reports(cli: Cli, world: World) -> None:
    _login(cli)
    out = cli("rankings", "--level", "ad", "--top", "5")
    assert "as of 2026-11-10" in out and "ad 0" in out
    out = cli("llm-usage")
    assert "today  1 calls" in out and "Phase 4" in out
    out = cli("changes", "--since", "30d")
    assert str(world.change) in out and "better" in out
    out = cli("alerts")
    assert "pacing_warn" in out
    assert "acked by admin" in cli("ack", str(world.alert))
    assert "(none)" in cli("alerts")
    assert "0.1.0" in cli("version")


def test_unreachable_api(tmp_path: Path) -> None:
    r = CliRunner().invoke(
        ads.app,
        [
            "--api-url",
            "http://127.0.0.1:9/api/v1",
            "--token-file",
            str(tmp_path / "t.json"),
            "login",
            "--username",
            "admin",
            "--password",
            "x",
        ],
    )
    assert r.exit_code == 1 and "cannot reach API" in r.output
