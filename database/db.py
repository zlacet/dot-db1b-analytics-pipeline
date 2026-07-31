"""
db.py

Single shared PostgreSQL connection helper used by every script in this
project (create_tables.py, ingest.py, validate.py, transform.py, load.py,
ai_layer/summarize.py, reports/generate_reports.py) instead of each one
defining its own copy of the same function.

Connection settings can be overridden with:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit(
        "Missing dependency: psycopg2-binary.\n"
        "Install it with: pip install psycopg2-binary --break-system-packages"
    )


def get_connection():
    """Open a connection to PostgreSQL using PG* environment variables,
    falling back to sensible local defaults."""
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "airline_fare_variance_platform"),
        user=os.environ.get("PGUSER", os.environ.get("USER", "postgres")),
        password=os.environ.get("PGPASSWORD"),
    )
