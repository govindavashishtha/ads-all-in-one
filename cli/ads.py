"""`ads` operator CLI (plan §19). Calls the API only — never the DB.

``ads login`` stores ``{api_url, token, ...}`` in a 0600 file (default:
``~/.config/ads-autopilot/credentials.json``; override with ``--token-file`` / ``ADS_TOKEN_FILE``).
The API URL is ``--api-url`` / ``ADS_API_URL`` or the one saved at login; behind the proxy it is
``https://<host>/ads-autopilot/api/v1``.

Exit codes: 0 ok · 1 API/other error · 2 usage · 3 not logged in / forbidden · 4 not enabled in
this phase (HTTP 501).
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import httpx
import typer

from api.schemas import OverviewResponse, SystemResponse
from engine.enums import Mode

DEFAULT_API_URL = "http://127.0.0.1:8000/api/v1"
EXIT_ERROR, EXIT_AUTH, EXIT_NOT_ENABLED = 1, 3, 4

app = typer.Typer(help="ads-autopilot operator CLI", no_args_is_help=True)

# Replaced in tests with a factory returning a FastAPI TestClient.
HttpFactory = Callable[[str], httpx.Client]


def _default_http(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)


http_factory: HttpFactory = _default_http


@dataclass
class Ctx:
    api_url: str | None
    token_file: Path
    as_json: bool


def _default_token_file() -> Path:
    return Path(typer.get_app_dir("ads-autopilot")) / "credentials.json"


@app.callback()
def _root(
    ctx: typer.Context,
    api_url: Annotated[
        str | None, typer.Option(envvar="ADS_API_URL", help="API base URL ending in /api/v1")
    ] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(envvar="ADS_TOKEN_FILE", help="where `ads login` stores the token"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="print raw JSON")] = False,
) -> None:
    """ads-autopilot operator CLI."""
    ctx.obj = Ctx(api_url=api_url, token_file=token_file or _default_token_file(), as_json=as_json)


# --------------------------------------------------------------------------- plumbing


def _fail(message: str, code: int = EXIT_ERROR) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _load_creds(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _fail(f"cannot read {path}: {exc}")
    return data if isinstance(data, dict) else {}


def _save_creds(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _detail(resp: httpx.Response) -> Any:
    try:
        return resp.json().get("detail", resp.text)
    except ValueError:
        return resp.text


class Api:
    def __init__(self, ctx: Ctx, *, need_token: bool = True) -> None:
        creds = _load_creds(ctx.token_file)
        self.base = (ctx.api_url or creds.get("api_url") or DEFAULT_API_URL).rstrip("/") + "/"
        self.token: str | None = creds.get("token")
        if need_token and not self.token:
            _fail("not logged in: run `ads login`", EXIT_AUTH)
        self.http = http_factory(self.base)
        self.ctx = ctx

    def call(self, method: str, path: str, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            resp = self.http.request(method, path.lstrip("/"), headers=headers, **kw)
        except httpx.HTTPError as exc:
            _fail(f"cannot reach API at {self.base}: {exc}")
        if resp.status_code == 501:
            d = _detail(resp)
            msg = d.get("message", d) if isinstance(d, dict) else d
            phase = d.get("phase", "?") if isinstance(d, dict) else "?"
            _fail(f"not enabled in this phase (ships in Phase {phase}): {msg}", EXIT_NOT_ENABLED)
        if resp.status_code == 401:
            _fail(f"not authenticated ({_detail(resp)}): run `ads login`", EXIT_AUTH)
        if resp.status_code == 403:
            _fail(f"forbidden: {_detail(resp)}", EXIT_AUTH)
        if resp.status_code >= 400:
            _fail(f"error {resp.status_code}: {_detail(resp)}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


def _ctx(ctx: typer.Context) -> Ctx:
    obj: Ctx = ctx.obj
    return obj


def _emit(ctx: Ctx, data: Any, render: Callable[[Any], None]) -> None:
    if ctx.as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        render(data)


def _table(rows: Sequence[dict[str, Any]], cols: Sequence[str]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    typer.echo("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


# --------------------------------------------------------------------------- auth


@app.command()
def login(
    ctx: typer.Context,
    username: Annotated[str, typer.Option(prompt=True)] = "admin",
    password: Annotated[str, typer.Option(prompt=True, hide_input=True)] = "",
    role: Annotated[str | None, typer.Option(help="down-scope: viewer|approver|admin")] = None,
) -> None:
    """Create a CLI session and store the bearer token (0600)."""
    c = _ctx(ctx)
    api = Api(c, need_token=False)
    body: dict[str, Any] = {"username": username, "password": password}
    if role:
        body["role"] = role
    data = api.call("POST", "auth/token", json=body)
    _save_creds(
        c.token_file,
        {
            "api_url": api.base.rstrip("/"),
            "token": data["access_token"],
            "username": data["username"],
            "role": data["role"],
            "expires_at": data["expires_at"],
        },
    )
    typer.echo(f"logged in as {data['username']} ({data['role']}) until {data['expires_at']}")


@app.command()
def logout(ctx: typer.Context) -> None:
    """Revoke the CLI session on the server and delete the local token."""
    c = _ctx(ctx)
    api = Api(c)
    try:
        api.call("POST", "auth/logout")
    finally:
        c.token_file.unlink(missing_ok=True)
    typer.echo("logged out")


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Show the current user and role."""
    c = _ctx(ctx)
    d = Api(c).call("GET", "auth/me")
    _emit(c, d, lambda d: typer.echo(f"{d['username']} ({d['role']}, {d['session_kind']})"))


# --------------------------------------------------------------------------- status


@app.command()
def status(ctx: typer.Context) -> None:
    """Mode, kill switch, spend vs caps, pace, open approvals, last job runs."""
    c = _ctx(ctx)
    api = Api(c)
    ov_raw = api.call("GET", "overview")
    sys_raw = api.call("GET", "system")
    if c.as_json:
        _emit(c, {"overview": ov_raw, "system": sys_raw}, lambda _: None)
        return
    ov = OverviewResponse.model_validate(ov_raw)
    sy = SystemResponse.model_validate(sys_raw)
    ks = ov.kill_switch
    typer.echo(f"client   {ov.client_name} ({ov.client_id}), currency {ov.currency}")
    typer.echo(f"mode     {ov.mode.value} (max {ov.hard_max_mode.value})")
    typer.echo(
        f"kill     {'ON' if ks.effective else 'off'}"
        + (" (env override)" if ks.env_override else "")
    )
    rows = []
    for b in [*ov.platforms, ov.total]:
        rows.append(
            {
                "platform": b.platform,
                "today": f"{b.spend_today} / {b.daily_cap or '-'}",
                "today%": _pct(b.spent_fraction_today),
                "day_elapsed": _pct(b.elapsed_day_fraction),
                "mtd": f"{b.mtd_spend} / {b.monthly_cap or '-'}",
                "mtd%": _pct(b.mtd_fraction),
                f"cpa_{ov.range_days}d": b.range.cpa,
                f"roas_{ov.range_days}d": None if b.range.roas is None else f"{b.range.roas:.2f}",
            }
        )
    typer.echo("")
    _table(rows, list(rows[0]))
    a = ov.open_alerts
    typer.echo("")
    typer.echo(
        f"approvals {ov.open_approvals} open · alerts {a.critical} critical / {a.warn} warn / "
        f"{a.info} info · emergency actions active {ov.active_emergency_actions}"
    )
    typer.echo("")
    typer.echo("last job runs:")
    _table(
        [j.model_dump(mode="json") for j in sy.latest_job_runs],
        ["job", "step", "window_key", "status", "started_at", "error"],
    )


# --------------------------------------------------------------------------- proposals


@app.command()
def proposals(
    ctx: typer.Context,
    status_: Annotated[str | None, typer.Option("--status")] = "awaiting_approval",
    all_: Annotated[bool, typer.Option("--all", help="any status")] = False,
) -> None:
    """List proposals (default: awaiting approval)."""
    c = _ctx(ctx)
    params = {} if all_ or not status_ else {"status": status_}
    d = Api(c).call("GET", "proposals", params=params)
    _emit(
        c,
        d,
        lambda d: _table(
            d,
            [
                "id",
                "status",
                "action",
                "entity_type",
                "entity_id",
                "params",
                "risk",
                "reason",
                "expires_at",
            ],
        ),
    )


@app.command()
def approve(
    ctx: typer.Context,
    ids: Annotated[list[int], typer.Argument(help="one or more proposal ids")],
    comment: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Approve proposals (each is still guardrailed individually)."""
    c = _ctx(ctx)
    api = Api(c)
    if len(ids) == 1:
        d = api.call("POST", f"proposals/{ids[0]}/approve", json={"comment": comment})
        _emit(
            c,
            d,
            lambda d: typer.echo(
                f"proposal {ids[0]}: {d['proposal']['status']} — {d['next_step']}"
            ),
        )
        return
    d = api.call("POST", "proposals/approve-batch", json={"ids": ids, "comment": comment})

    def render(d: Any) -> None:
        for r in d["results"]:
            typer.echo(
                f"proposal {r['proposal_id']}: {r['status'] if r['ok'] else 'FAILED ' + r['error']}"
            )
        typer.echo(d["next_step"])

    _emit(c, d, render)
    if not all(r["ok"] for r in d["results"]):
        raise typer.Exit(EXIT_ERROR)


@app.command()
def reject(
    ctx: typer.Context,
    proposal_id: int,
    reason: Annotated[str, typer.Option(help="why (stored with the decision)")],
) -> None:
    """Reject a proposal."""
    c = _ctx(ctx)
    d = Api(c).call("POST", f"proposals/{proposal_id}/reject", json={"reason": reason})
    _emit(c, d, lambda d: typer.echo(f"proposal {proposal_id}: {d['proposal']['status']}"))


@app.command("dry-run")
def dry_run(ctx: typer.Context, proposal_id: int) -> None:
    """What the executor would send for a proposal (nothing is sent or changed)."""
    c = _ctx(ctx)
    d = Api(c).call("GET", f"proposals/{proposal_id}/dry-run")

    def render(d: Any) -> None:
        g = d["guardrail"]
        typer.echo(
            f"proposal {d['proposal_id']} {d['action']} ({d['status']}): guardrail "
            f"{g.get('result')}{' ' + g['reason'] if g.get('reason') else ''}"
        )
        for op in d["ops"]:
            typer.echo(
                f"  {op['platform']} {op['entity_type']} {op['external_id']}: {op['op']} "
                f"{json.dumps(op['args'] or op['intended'])}"
                f"{'' if op['would_change'] else '  (already so: no-op)'}"
            )
        if d.get("plan_error"):
            typer.echo(f"  plan error: {d['plan_error']}")
        e = d["execution"]
        typer.echo(
            "write gate: "
            + ("open" if e["write_gate_open"] else f"CLOSED — {e['write_gate_reason']}")
        )

    _emit(c, d, render)


def _queued(d: Any) -> None:
    typer.echo(
        f"queued {d['kind']} as write request #{d['id']} — the engine runs it within a "
        f"minute; check with `ads write-request {d['id']}`"
    )


# --------------------------------------------------------------------------- changes


@app.command()
def changes(ctx: typer.Context, since: Annotated[str, typer.Option()] = "7d") -> None:
    """Change feed with verdicts."""
    c = _ctx(ctx)
    d = Api(c).call("GET", "changes", params={"since": since})
    _emit(
        c,
        d,
        lambda d: _table(
            d,
            [
                "id",
                "started_at",
                "platform",
                "action",
                "entity_type",
                "entity_id",
                "status",
                "verdict",
            ],
        ),
    )


@app.command()
def rollback(
    ctx: typer.Context,
    change_id: int,
    yes: Annotated[bool, typer.Option("--yes", help="skip the confirmation prompt")] = False,
) -> None:
    """Roll back a change (§15)."""
    c = _ctx(ctx)
    if not yes:
        typer.confirm(f"Roll back change {change_id}?", abort=True)
    d = Api(c).call("POST", f"changes/{change_id}/rollback")
    _emit(c, d, _queued)


@app.command("pause-all")
def pause_all(
    ctx: typer.Context,
    platform: Annotated[str | None, typer.Option(help="meta|google (default: both)")] = None,
    confirm: Annotated[bool, typer.Option("--confirm", help="required")] = False,
) -> None:
    """Emergency pause of managed campaigns (restore_policy = manual)."""
    c = _ctx(ctx)
    if not confirm:
        _fail("refusing without --confirm", 2)
    d = Api(c).call("POST", "control/pause-all", json={"platform": platform, "confirm": True})
    _emit(c, d, _queued)


@app.command()
def restore(ctx: typer.Context, emergency_action_id: int) -> None:
    """Restore campaigns paused by an emergency action."""
    c = _ctx(ctx)
    d = Api(c).call("POST", f"emergency-actions/{emergency_action_id}/restore")
    _emit(c, d, _queued)


@app.command("write-request")
def write_request(ctx: typer.Context, request_id: int) -> None:
    """Status/result of a queued rollback, restore or pause-all."""
    c = _ctx(ctx)
    d = Api(c).call("GET", f"write-requests/{request_id}")

    def render(d: Any) -> None:
        typer.echo(f"write request #{d['id']} {d['kind']}: {d['status']}")
        if d.get("error"):
            typer.echo(f"error: {d['error']}")
        if d.get("result"):
            typer.echo(json.dumps(d["result"], indent=2))

    _emit(c, d, render)


# --------------------------------------------------------------------------- control


def _on_off(value: str) -> bool:
    v = value.lower()
    if v not in ("on", "off"):
        _fail("expected on|off", 2)
    return v == "on"


@app.command()
def manage(
    ctx: typer.Context,
    campaign_id: int,
    on_off: Annotated[str, typer.Argument(metavar="on|off")],
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Opt a campaign in/out of automation."""
    c = _ctx(ctx)
    managed = _on_off(on_off)
    d = Api(c).call(
        "PUT", f"campaigns/{campaign_id}/managed", json={"managed": managed, "reason": reason}
    )
    _emit(c, d, lambda d: typer.echo(f"campaign {d['id']} managed={d['managed']}"))


@app.command()
def mode(
    ctx: typer.Context,
    new_mode: Annotated[Mode, typer.Argument(metavar="read_only|suggest|auto_small|auto")],
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Set the operating mode (bounded by HARD_MAX_MODE)."""
    c = _ctx(ctx)
    d = Api(c).call("POST", "control/mode", json={"mode": new_mode.value, "reason": reason})
    _emit(c, d, lambda d: typer.echo(f"mode {d['mode']} (max {d['hard_max_mode']})"))


@app.command("kill-switch")
def kill_switch(
    ctx: typer.Context,
    on_off: Annotated[str, typer.Argument(metavar="on|off")],
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Turn the DB kill switch on/off (cannot clear the env override)."""
    c = _ctx(ctx)
    d = Api(c).call("POST", "control/kill-switch", json={"on": _on_off(on_off), "reason": reason})

    def render(d: Any) -> None:
        typer.echo(
            f"kill switch {'ON' if d['effective'] else 'off'} "
            f"(db={d['db']}, env={d['env_override']})"
        )
        if d.get("note"):
            typer.echo(d["note"])

    _emit(c, d, render)


@app.command()
def execution(
    ctx: typer.Context,
    on_off: Annotated[str | None, typer.Argument(metavar="[on|off]")] = None,
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Show or set the platform-write master switch (clients.execution_enabled; admin)."""
    c = _ctx(ctx)
    if on_off is None:
        d = Api(c).call("GET", "control/execution")
    else:
        d = Api(c).call(
            "POST", "control/execution", json={"enabled": _on_off(on_off), "reason": reason}
        )

    def render(d: Any) -> None:
        typer.echo(
            f"execution {'ON' if d['execution_enabled'] else 'off'}, mode {d['mode']} "
            f"(max {d['hard_max_mode']}), write gate "
            + ("open" if d["write_gate_open"] else f"CLOSED — {d['write_gate_reason']}")
        )

    _emit(c, d, render)


@app.command()
def run(ctx: typer.Context, job: str) -> None:
    """Queue a job run; the engine picks it up within about a minute."""
    c = _ctx(ctx)
    d = Api(c).call("POST", f"jobs/{job}/run")
    _emit(
        c,
        d,
        lambda d: typer.echo(
            f"queued {d['job']} as request #{d['id']} ({d['status']}); "
            "the engine scheduler runs it within ~1 minute"
        ),
    )


# --------------------------------------------------------------------------- reports


@app.command()
def rankings(
    ctx: typer.Context,
    level: Annotated[str, typer.Option(help="ad|ad_group|keyword|campaign")] = "ad",
    top: Annotated[int, typer.Option()] = 20,
    platform: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Ranked table with intervals."""
    c = _ctx(ctx)
    params: dict[str, Any] = {"level": level, "top": top}
    if platform:
        params["platform"] = platform
    d = Api(c).call("GET", "rankings", params=params)

    def render(d: Any) -> None:
        typer.echo(f"{d['level']} rankings as of {d['as_of'] or '(no scores yet)'}")
        _table(
            d["rows"],
            [
                "peer_group",
                "rank_in_peer",
                "entity_id",
                "platform",
                "name",
                "cpa_p10",
                "cpa_shrunk",
                "cpa_p90",
                "p_beats_target",
                "data_sufficient",
            ],
        )

    _emit(c, d, render)


@app.command("llm-usage")
def llm_usage(ctx: typer.Context) -> None:
    """LLM tokens/cost today and month-to-date vs caps."""
    c = _ctx(ctx)
    d = Api(c).call("GET", "llm/usage")

    def render(d: Any) -> None:
        t, m = d["today"], d["month_to_date"]
        typer.echo(
            f"today  {t['calls']} calls, {t['input_tokens'] + t['output_tokens']} tokens "
            f"(cap {d['daily_token_cap']}), ${t['cost_usd']} (cap ${d['daily_cost_cap_usd']})"
        )
        typer.echo(
            f"MTD    {m['calls']} calls, ${m['cost_usd']} (cap ${d['monthly_cost_cap_usd']})"
        )
        if not d["llm_enabled"]:
            typer.echo("LLM layer not enabled yet (Phase 4)")

    _emit(c, d, render)


@app.command()
def alerts(
    ctx: typer.Context, all_: Annotated[bool, typer.Option("--all", help="include acked")] = False
) -> None:
    """List open alerts."""
    c = _ctx(ctx)
    d = Api(c).call("GET", "alerts", params={"open": str(not all_).lower()})
    _emit(
        c, d, lambda d: _table(d, ["id", "severity", "kind", "message", "created_at", "acked_by"])
    )


@app.command()
def ack(ctx: typer.Context, alert_id: int) -> None:
    """Acknowledge an alert."""
    c = _ctx(ctx)
    d = Api(c).call("POST", f"alerts/{alert_id}/ack")
    _emit(c, d, lambda d: typer.echo(f"alert {d['id']} acked by {d['acked_by']}"))


@app.command()
def version() -> None:
    """Print the CLI version."""
    typer.echo("ads-autopilot 0.1.0")


if __name__ == "__main__":
    app()
