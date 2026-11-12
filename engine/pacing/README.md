# engine/pacing

Owner: executor/evaluator agent. `curve.py` (§12.4 step 2), `emergency.py` (the only
non-executor module allowed to import WriteConnector): pause managed active campaigns,
pause-all, restore (D-28, D-33). Wired to `engine.jobs.pacing.EMERGENCY_HANDLER`. Emergency
rows list only applied pauses; restore also re-checks the monthly cap.
