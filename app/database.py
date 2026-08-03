"""
Database connection setup.
Reads connection info from environment variables with sane local defaults.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
# Defaults match docker-compose.yml's published port (5435:5432), not
# Postgres's stock 5432 -- the compose file remaps to avoid colliding with
# other local Postgres instances. Override with POSTGRES_PORT if needed.
DB_PORT = os.getenv("POSTGRES_PORT", "5435")
DB_NAME = os.getenv("POSTGRES_DB", "flashsale")

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# pool_size kept modest; demos open their own short-lived connections
# where explicit transaction control (isolation level, locking) is needed.
engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, future=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
