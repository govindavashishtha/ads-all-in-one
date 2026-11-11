# engine/db

SQLAlchemy 2 models (§8.3) + engine/session helpers. Schema changes = models + a new Alembic revision in db/migrations/versions (never edit 0001 once others depend on it).
