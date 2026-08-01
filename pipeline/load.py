"""
load.py

Takes the three analytics result sets computed by transform.py and loads
them into their PostgreSQL tables:
    route_summary, carrier_summary, price_variance_summary

Each table is truncated and reloaded in full on every run, since these are
derived/aggregated tables meant to always reflect the current state of
raw_airline_data - not appended to incrementally.

Blank/NaN values (e.g. passenger_growth_pct for a route's first quarter,
which has no prior quarter to compare against) are written as real SQL
NULLs, not zeros.

Connection settings (same as ingest.py/transform.py) can be overridden with:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Usage:
    python load.py
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "database"))

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: pandas.\nInstall it with: pip install pandas --break-system-packages")

from db import get_connection
import transform

TABLE_COLUMNS = {
    "route_summary": [
        "route", "year", "quarter", "passengers", "passenger_growth_pct",
        "average_price", "price_per_mile", "price_change_pct",
    ],
    "carrier_summary": [
        "route", "carrier", "year", "quarter", "passengers",
        "average_price", "price_per_mile", "route_share_pct",
    ],
    "price_variance_summary": [
        "route", "year", "quarter", "current_average_price",
        "previous_average_price", "price_change_pct", "current_price_per_mile",
        "previous_price_per_mile", "price_per_mile_change_pct",
    ],
}


def load_table(cur, table: str, df: pd.DataFrame) -> None:
    """Truncate a table and COPY a DataFrame into it, in the column order
    the table expects. NaN values are written as empty fields, which COPY
    with NULL '' turns into real SQL NULLs rather than the literal text
    "nan"."""
    columns = TABLE_COLUMNS[table]
    ordered = df[columns]

    buffer = io.StringIO()
    ordered.to_csv(buffer, index=False, header=False, na_rep="")
    buffer.seek(0)

    cur.execute(f"TRUNCATE {table};")
    cur.copy_expert(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, NULL '')",
        buffer,
    )


def load_all(conn, route_summary_df: pd.DataFrame, carrier_summary_df: pd.DataFrame,
             price_variance_df: pd.DataFrame) -> None:
    """Load all three already-computed result sets into their tables and
    print row counts. Split out from main() so main.py (the orchestrator)
    can compute transform's result sets once and hand them straight here,
    instead of main.py calling transform.py and load.py separately and
    triggering the (expensive) transformation twice."""
    with conn.cursor() as cur:
        print("\nLoading route_summary...")
        load_table(cur, "route_summary", route_summary_df)

        print("Loading carrier_summary...")
        load_table(cur, "carrier_summary", carrier_summary_df)

        print("Loading price_variance_summary...")
        load_table(cur, "price_variance_summary", price_variance_df)

    conn.commit()

    print("\nRow counts after load:")
    with conn.cursor() as cur:
        for table in TABLE_COLUMNS:
            cur.execute(f"SELECT COUNT(*) FROM {table};")
            count = cur.fetchone()[0]
            print(f"  {table}: {count:,} rows")


def main() -> None:
    """Standalone entry point: computes the transformations itself, then
    loads them. (When run as part of main.py's orchestrated pipeline,
    load_all() is called directly with already-computed DataFrames instead,
    to avoid computing the transformations twice.)"""
    conn = get_connection()
    try:
        print("Running transformations...")
        route_summary_df, carrier_summary_df, price_variance_df = transform.run_transformations(conn)
        load_all(conn, route_summary_df, carrier_summary_df, price_variance_df)
        print("\nLoad complete.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
