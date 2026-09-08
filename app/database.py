"""
Database connection setup.
Reads connection info from environment variables with sane local defaults.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# A full connection string, when set, wins outright -- this is how a managed
# Postgres (Neon, Supabase, RDS, ...) is wired up for the Vercel deployment,
# where there is no docker-compose Postgres to default to. Neon's connection
# strings come as postgresql://..., which SQLAlchemy needs rewritten to the
# psycopg2 dialect URL; ?sslmode=require (Neon requires TLS) passes through
# untouched since it's already a query param on the string.
_env_url = os.getenv("DATABASE_URL")

if _env_url:
    DATABASE_URL = _env_url.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    DB_USER = os.getenv("POSTGRES_USER", "postgres")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
    DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
    # Defaults match docker-compose.yml's published port (5435:5432), not
    # Postgres's stock 5432 -- the compose file remaps to avoid colliding with
    # other local Postgres instances. Override with POSTGRES_PORT if needed.
    DB_PORT = os.getenv("POSTGRES_PORT", "5435")
    DB_NAME = os.getenv("POSTGRES_DB", "flashsale")
    DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# On Vercel each request can land in a fresh function instance, so a pool
# sized for a long-lived process just means most connections are opened once
# and never reused -- pool_pre_ping avoids handing out one a serverless
# platform has already killed out from under us. Neon's own pooler (the
# "-pooler" host in its connection string) is what actually absorbs
# concurrent serverless invocations; this pool is deliberately small on top
# of that, not a replacement for it.
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=bool(_env_url),
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
