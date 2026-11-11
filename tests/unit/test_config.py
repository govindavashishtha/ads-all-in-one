from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.config import Settings
from engine.enums import Mode, Role
from engine.errors import MissingInputError

ROOT = Path(__file__).resolve().parents[2]


def _env_keys(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^([A-Z0-9_]+)=", text, re.M)]


def test_env_example_is_exactly_plan_section_7_2() -> None:
    plan = (ROOT / "docs" / "BUILD_PLAN.md").read_text(encoding="utf-8")
    block = plan.split("### 7.2 `.env.example`", 1)[1].split("```dotenv\n", 1)[1].split("```", 1)[0]
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == block


def test_every_env_key_is_a_setting() -> None:
    keys = _env_keys((ROOT / ".env.example").read_text(encoding="utf-8"))
    fields = {f.upper() for f in Settings.model_fields}
    assert set(keys) <= fields, set(keys) - fields
    assert fields <= set(keys), fields - set(keys)


def test_env_example_parses_with_blanks_as_defaults() -> None:
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.initial_mode is Mode.READ_ONLY
    assert s.hard_max_mode is Mode.SUGGEST
    assert s.hard_daily_cap is None
    assert s.kill_switch is False
    assert s.llm_small_model is None  # never defaulted (INPUT I-8)
    assert s.llm_daily_cost_cap_usd == Decimal("1.00")


def test_hard_caps_required(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(hard_daily_cap=None, hard_monthly_cap=None)
    with pytest.raises(MissingInputError) as exc:
        s.require_hard_caps()
    assert exc.value.input_id == "I-2"
    assert make_settings().require_hard_caps() == (Decimal("100000"), Decimal("3000000"))


def test_initial_mode_cannot_exceed_hard_max_mode(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError):
        make_settings(initial_mode="auto_small", hard_max_mode="suggest")


def test_mode_ceiling_and_kill_switch_override(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(hard_max_mode="suggest", kill_switch=True)
    assert s.mode_allowed(Mode.SUGGEST)
    assert not s.mode_allowed(Mode.AUTO_SMALL)
    assert s.effective_kill_switch(db_kill_switch=False) is True
    s2 = make_settings(kill_switch=False)
    assert s2.effective_kill_switch(db_kill_switch=True) is True
    assert s2.effective_kill_switch(db_kill_switch=False) is False


def test_invalid_timezone_and_caps(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError):
        make_settings(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        make_settings(hard_daily_cap="0")


def test_mode_and_role_ordering() -> None:
    assert Mode.AUTO.exceeds(Mode.AUTO_SMALL)
    assert not Mode.READ_ONLY.exceeds(Mode.SUGGEST)
    assert Role.ADMIN.includes(Role.APPROVER)
    assert not Role.VIEWER.includes(Role.APPROVER)


def test_secrets_not_in_repr(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(meta_read_token="EAAsupersecretvalue")
    assert "supersecret" not in repr(s)
