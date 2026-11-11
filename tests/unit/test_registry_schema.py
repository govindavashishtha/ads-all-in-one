"""Registry ⇄ schema consistency (plan §11.1, §22 "Registry")."""

from __future__ import annotations

from engine.bootstrap import DEFAULT_RULES_PATH, load_yaml
from engine.db.models import Feature, Score
from engine.rules.registry import ALLOWED_FUNCTIONS, NAMES, REGISTRY, names_from
from engine.rules.schema import expression_names

KEY_COLUMNS = {"client_id", "entity_type", "entity_id", "as_of", "created_at"}


def test_registry_names_unique() -> None:
    assert len(NAMES) == len(REGISTRY)


def test_features_table_has_exactly_one_column_per_feature_variable() -> None:
    cols = {c.name for c in Feature.__table__.columns} - KEY_COLUMNS
    assert cols == names_from("features")


def test_score_variables_exist_in_scores_table() -> None:
    cols = {c.name for c in Score.__table__.columns}
    assert names_from("scores") <= cols


def test_every_seed_rule_uses_registry_names_only() -> None:
    for rule in load_yaml(DEFAULT_RULES_PATH)["rules"]:
        assert expression_names(rule["when"]) <= NAMES | ALLOWED_FUNCTIONS, rule["key"]
