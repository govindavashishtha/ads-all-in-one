"""`ads` operator CLI (plan §19). Calls the API only (bearer token from `ads login`, 0600 file).

Commands are added by the API/CLI agent. Never import engine.db / SQLAlchemy here.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="ads-autopilot operator CLI", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the CLI version."""
    typer.echo("ads-autopilot 0.1.0")


@app.callback()
def _root() -> None:
    """ads-autopilot operator CLI."""


if __name__ == "__main__":
    app()
