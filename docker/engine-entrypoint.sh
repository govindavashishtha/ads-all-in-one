#!/bin/sh
# Engine start: migrate to head, then run the engine (which bootstraps on first start, §7.1).
set -eu
alembic -c db/alembic.ini upgrade head
exec python -m engine.main
