"""PostgreSQL connection helper.

Supports two modes:
  1. Render / hosted: reads DATABASE_URL (connection URI with sslmode).
  2. Local / .env:    reads DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.

The hosted DATABASE_URL takes precedence when set.  Local behaviour is
unchanged — the existing .env file continues to work exactly as before.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_connection() -> psycopg.Connection:
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        # Render provides an internal connection string.  Ensure SSL when the
        # URL does not already specify sslmode.
        if "sslmode=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url += f"{separator}sslmode=require"
        return psycopg.connect(database_url, connect_timeout=10)

    # Fallback: individual env-var connection (local development).
    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "energy_monitor"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )


if __name__ == "__main__":
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name;
                """)
                print("Connected to PostgreSQL!")
                for row in cur.fetchall():
                    print(row[0])
    except Exception as e:
        print("Connection failed:", e)
