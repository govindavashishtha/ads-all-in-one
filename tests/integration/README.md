# Integration tests

Need `TEST_DATABASE_URL` pointing at a disposable Postgres 16 database (the session fixture
downgrades it to base and re-migrates). Platform integration tests (§22: Google test account,
Meta paused campaign) are added later and must be opt-in via markers — never run by default.
