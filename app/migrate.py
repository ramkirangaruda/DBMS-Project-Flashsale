"""
The small, idempotent piece of schema evolution `Base.metadata.create_all()`
cannot do for us: adding a column to a table that already exists.

WHY THIS FILE EXISTS INSTEAD OF A REAL ALEMBIC MIGRATION
    `alembic` has been in requirements.txt since early in this project, but
    it was never actually initialized -- there is no alembic.ini, no
    migrations/ directory, no revision history anywhere in this repo.
    Every schema change so far has gone through `create_all()` alone
    (app/main.py's startup event, and the three seed scripts), which is
    sufficient for adding NEW tables but silently does nothing for altering
    an EXISTING one -- it only issues CREATE TABLE IF NOT EXISTS per model,
    never ALTER TABLE.

    Standing up a full Alembic environment now, for the sake of one nullable
    column, would be a disproportionate new piece of machinery inconsistent
    with how every other schema change in this project has been made -- and
    it would still need this exact same one-time backfill logic to handle
    the column not existing on a database that predates it. This function
    is that logic, kept small and named for what it does rather than
    dressed up as more than it is.

WHAT IT DOES
    `orders.strategy` (see app/models.py's Order docstring) is added with
    `ADD COLUMN IF NOT EXISTS`, which is a no-op on every call after the
    first -- safe to run on every startup, on every replica, forever.
"""
from sqlalchemy import text


def run(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy VARCHAR(20)"
        ))
