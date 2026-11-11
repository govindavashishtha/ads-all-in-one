"""Import-boundary lint (plan §0 rules 3–4, §20, §22 "Boundary").

1. Only engine/executor/** and engine/pacing/emergency.py may reference WriteConnector or
   import a *_write connector module (engine/connectors itself defines them).
2. Only engine/connectors/** may import the platform SDKs.
3. Only engine/connectors/** may import engine.connectors.money (money conversion).
4. cli/** talks to the API only: no SQLAlchemy, psycopg, engine.db or connectors.
5. Only engine/config.py reads environment variables (secrets flow via settings ->
   credentials provider).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("engine", "api", "cli")

WRITE_ALLOWED_PREFIXES = ("engine/executor/", "engine/connectors/")
WRITE_ALLOWED_FILES = {"engine/pacing/emergency.py"}
WRITE_MODULES = {"engine.connectors.meta_write", "engine.connectors.google_write"}
SDK_PREFIXES = ("facebook_business", "google.ads", "google.analytics")
CLI_FORBIDDEN = ("sqlalchemy", "psycopg", "alembic", "engine.db", "engine.connectors")
ENV_ALLOWED_FILES = {"engine/config.py"}
ENV_ATTRS = {"environ", "environb", "getenv"}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _module_name(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _imports(tree: ast.AST, rel: str) -> list[tuple[int, str, list[str]]]:
    """(line, absolute module, imported names)."""
    out: list[tuple[int, str, list[str]]] = []
    package = _module_name(rel)
    if not rel.endswith("__init__.py"):
        package = package.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, a.name, []) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parent = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parent, base] if base else parent)
            names = [a.name for a in node.names]
            out.append((node.lineno, base, names))
            out.extend((node.lineno, f"{base}.{n}", []) for n in names)
    return out


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def check_source(rel: str, source: str) -> list[Violation]:
    tree = ast.parse(source, filename=rel)
    v: list[Violation] = []
    write_ok = rel.startswith(WRITE_ALLOWED_PREFIXES) or rel in WRITE_ALLOWED_FILES
    in_connectors = rel.startswith("engine/connectors/")

    for line, module, names in _imports(tree, rel):
        if not write_ok and (module in WRITE_MODULES or "WriteConnector" in names):
            v.append(Violation(rel, line, f"WriteConnector import outside executor: {module}"))
        if not in_connectors and any(_matches(module, p) for p in SDK_PREFIXES):
            v.append(Violation(rel, line, f"platform SDK import outside connectors: {module}"))
        if not in_connectors and _matches(module, "engine.connectors.money"):
            v.append(Violation(rel, line, "money conversion outside connectors"))
        if rel.startswith("cli/") and any(_matches(module, p) for p in CLI_FORBIDDEN):
            v.append(Violation(rel, line, f"CLI must call the API, not import {module}"))

    for node in ast.walk(tree):
        if not write_ok and (
            (isinstance(node, ast.Name) and node.id == "WriteConnector")
            or (isinstance(node, ast.Attribute) and node.attr == "WriteConnector")
        ):
            v.append(Violation(rel, node.lineno, "WriteConnector referenced outside executor"))
        if (
            rel not in ENV_ALLOWED_FILES
            and isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in ENV_ATTRS
        ):
            v.append(Violation(rel, node.lineno, "env access outside engine/config.py"))
    return v


def scan_repo() -> list[Violation]:
    violations: list[Violation] = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            violations.extend(check_source(rel, path.read_text(encoding="utf-8")))
    return violations


def test_repo_respects_import_boundaries() -> None:
    violations = scan_repo()
    assert not violations, "\n".join(map(str, violations))


# --- the checker itself must catch violations -------------------------------------------


def test_detects_write_connector_outside_executor() -> None:
    src = "from engine.connectors.base import WriteConnector\n"
    assert check_source("engine/rules/engine.py", src)
    assert not check_source("engine/executor/apply.py", src)
    assert not check_source("engine/pacing/emergency.py", src)
    assert check_source("engine/pacing/curve.py", src)


def test_detects_write_module_and_attribute_access() -> None:
    assert check_source("engine/jobs/daily.py", "from engine.connectors import meta_write\n")
    assert check_source("api/routes/x.py", "import engine.connectors.google_write\n")
    src = "import engine.connectors.base as b\nX = b.WriteConnector\n"
    assert check_source("engine/llm/budget.py", src)


def test_detects_relative_write_import() -> None:
    src = "from ..connectors.base import WriteConnector\n"
    assert check_source("engine/rules/engine.py", src)


def test_detects_sdk_and_money_outside_connectors() -> None:
    assert check_source("engine/ingest/pull.py", "from facebook_business.api import X\n")
    assert check_source("engine/ingest/pull.py", "import google.ads.googleads.client\n")
    assert not check_source("engine/connectors/meta_read.py", "import facebook_business\n")
    assert check_source(
        "engine/features/x.py", "from engine.connectors.money import quantize_major\n"
    )


def test_detects_cli_db_access_and_env_reads() -> None:
    assert check_source("cli/ads.py", "from engine.db.models import Client\n")
    assert check_source("cli/ads.py", "import sqlalchemy\n")
    assert check_source("engine/jobs/x.py", "import os\nT = os.environ['META_READ_TOKEN']\n")
    assert not check_source("engine/config.py", "import os\nT = os.getenv('X')\n")
