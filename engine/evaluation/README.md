# engine/evaluation

Owner: executor/evaluator agent. D+3 safety check, D+7 / D+14 Gamma-Poisson verdict (§16),
seed = `change_log.id` (D-32, D-34). `units.py` picks the evaluation unit; `verdict.py` is
pure; `evaluate.py` writes the verdict, records `rule_stats` at D+7/D+14, and rolls back
through the executor when D+3 or D+7 is `worse`.
