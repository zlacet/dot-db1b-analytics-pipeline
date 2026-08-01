"""
create_tables.py

Connects to PostgreSQL and executes schema.sql to create (or verify) all
tables for the Airline Price Variance Platform:
    raw_airline_data, route_summary, carrier_summary, price_variance_summary,
    ingest_log, validated_quarters

Connection settings can be overridden with environment variables:
    PGHOST      (default: localhost)
    PGPORT      (default: 5432)
    PGDATABASE  (default: airline_price_variance_platform)
    PGUSER      (default: current OS user)
    PGPASSWORD  (default: none / not required for local trust auth)

Usage:
    python create_tables.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

EXPECTED_TABLES = [
    "raw_airline_data",
    "route_summary",
    "carrier_summary",
    "price_variance_summary",
    "ingest_log",
    "validated_quarters",
]


def run_schema(conn) -> None:
    """Execute every statement in schema.sql against the given connection."""
    if not SCHEMA_FILE.exists():
        sys.exit(f"schema.sql not found at {SCHEMA_FILE}")

    sql = SCHEMA_FILE.read_text()

    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)

    print(f"Executed schema from {SCHEMA_FILE.name}")


def verify_tables(conn) -> None:
    """Confirm the expected tables now exist in the public schema."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name;
            """
        )
        found = {row[0] for row in cur.fetchall()}

    print("\nTables present in database:")
    for table in EXPECTED_TABLES:
        status = "OK" if table in found else "MISSING"
        print(f"  [{status}] {table}")

    missing = [t for t in EXPECTED_TABLES if t not in found]
    if missing:
        sys.exit(f"\nMissing tables: {', '.join(missing)}")


def main() -> None:
    print("Connecting to PostgreSQL...")
    conn = get_connection()
    try:
        print("Creating tables (if not already present)...")
        run_schema(conn)
        verify_tables(conn)
        print("\nDatabase setup complete.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
