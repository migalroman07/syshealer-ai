import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, sessionmaker

from src.config import BASE_DIR, load_config

config = load_config()
db_type = config.get("db_type", "sqlite")

if db_type == "sqlite":
    db_path = os.path.join(BASE_DIR, "syshealer.db")
    SQLALCHEMY_DATABASE_URL = f"sqlite:///{db_path}"
    # check_same_thread=False for simultaneous daemon and tui work.
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

    if not SQLALCHEMY_DATABASE_URL or not SQLALCHEMY_DATABASE_URL.startswith(
        "postgresql"
    ):
        print("[-] ERROR: Valid PostgreSQL DATABASE_URL is missing in the .env file.")
        sys.exit(1)

    try:
        url_obj = make_url(SQLALCHEMY_DATABASE_URL)
        target_db = url_obj.database

        if not re.match(r"^[a-zA-Z0-9_]+$", target_db):
            print("[-] ERROR: Invalid database name.")
            sys.exit(1)

        admin_url = url_obj.set(database="postgres")
        temp_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with temp_engine.connect() as conn:
                exists = conn.execute(
                    text(f"SELECT 1 FROM pg_database WHERE datname = '{target_db}'")
                ).scalar()
                if not exists:
                    conn.execute(text(f'CREATE DATABASE "{target_db}"'))
        finally:
            temp_engine.dispose()
    except Exception as e:
        print(f"[-] Database initialization error: {e}")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    raw_log: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    log_hash: Mapped[str | None] = mapped_column(
        String, unique=True, index=True, nullable=True
    )
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    executed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_log_review: Mapped[str | None] = mapped_column(Text, nullable=True)


try:
    Base.metadata.create_all(bind=engine)
except OperationalError:
    print(f"\n[-] CRITICAL ERROR: Could not connect to {db_type.upper()}!")
    sys.exit(1)
