"""
validate.py

Runs data quality checks against raw_airline_data and appends the results
to data/validation/validation_report.csv.

Checks performed:
  Missing data     - NULL/blank MktFare, Passengers, OpCarrier, Origin/Dest
  Data integrity   - Passengers <= 0, MktFare <= 0, MktMilesFlown <= 0
  Duplicate keys   - (ItinID, MktID) should be unique per DB1BMarket spec

Missing-data and duplicate-key failures are treated as critical: if any are
found, this script exits with a non-zero status so main.py can halt the
pipeline before transform.py runs on bad data. Integrity failures (e.g. a
handful of zero-price promotional records) are reported but do not block
the pipeline - transform.py is expected to exclude non-positive values
from its aggregations.

Validation is incremental: a validated_quarters table records which
(year, quarter) pairs have already passed missing-data/integrity checks.
Each run only re-checks quarters not yet in that table (i.e. whatever was
just ingested), rather than re-scanning the whole table every time. The
duplicate-key check stays table-wide regardless - it's cheap, and it's
already structurally guaranteed to be zero by raw_airline_data's primary
key, so it's really just a confirmation for the audit trail.
Newly-checked quarters are only marked validated if no critical failures
were found. The report file is appended to, not overwritten, so it keeps
a full history across every run.

Connection settings (same as ingest.py / create_tables.py) can be
overridden with: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Usage:
    python validate.py
"""

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.exit(
        "Missing dependency: psycopg2-binary.\n"
        "Install it with: pip install psycopg2-binary --break-system-packages"
    )

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "database"))
from db import get_connection

REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "validation"
REPORT_FILE = REPORT_DIR / "validation_report.csv"

# (check_name, description, category)
# category is one of: missing_data, integrity, duplicates
CHECKS = [
    ("missing_mkt_fare", "Rows with NULL MktFare", "missing_data"),
    ("missing_passengers", "Rows with NULL Passengers", "missing_data"),
    ("missing_carrier", "Rows with NULL or blank OpCarrier", "missing_data"),
    ("missing_airports", "Rows with NULL or blank Origin/Dest", "missing_data"),
    ("non_positive_passengers", "Rows with Passengers <= 0", "integrity"),
    ("non_positive_fare", "Rows with MktFare <= 0", "integrity"),
    ("non_positive_distance", "Rows with MktMilesFlown <= 0", "integrity"),
    ("duplicate_itin_mkt", "Excess rows for duplicate (ItinID, MktID) pairs", "duplicates"),
]

CRITICAL_CATEGORIES = {"missing_data", "duplicates"}


def ensure_tracking_table(cur) -> None:
    """Create the validated_quarters tracking table if it doesn't already
    exist (also defined in schema.sql; created here too so validate.py is
    safe to run standalone)."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS validated_quarters (
            year            SMALLINT        NOT NULL,
            quarter         SMALLINT        NOT NULL,
            validated_at    TIMESTAMPTZ     NOT NULL DEFAULT now(),
            CONSTRAINT pk_validated_quarters PRIMARY KEY (year, quarter)
        );
        """
    )


def get_already_validated(cur) -> set:
    cur.execute("SELECT year, quarter FROM validated_quarters;")
    return {(row[0], row[1]) for row in cur.fetchall()}


def get_available_quarters(cur) -> set:
    cur.execute("SELECT DISTINCT year, quarter FROM raw_airline_data;")
    return {(row[0], row[1]) for row in cur.fetchall()}


def run_checks(conn, quarters: list) -> dict:
    """Run missing-data/integrity checks scoped to the given (year, quarter)
    pairs, plus a table-wide duplicate check. quarters must be non-empty."""
    values_clause = ", ".join(["(%s, %s)"] * len(quarters))
    scope_params = [v for pair in quarters for v in pair]

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM raw_airline_data WHERE (year, quarter) IN (VALUES {values_clause});",
            scope_params,
        )
        total_rows = cur.fetchone()[0]

        cur.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE mkt_fare IS NULL)                                  AS missing_mkt_fare,
                COUNT(*) FILTER (WHERE passengers IS NULL)                                AS missing_passengers,
                COUNT(*) FILTER (WHERE op_carrier IS NULL OR TRIM(op_carrier) = '')        AS missing_carrier,
                COUNT(*) FILTER (
                    WHERE origin IS NULL OR TRIM(origin) = ''
                       OR dest IS NULL OR TRIM(dest) = ''
                )                                                                          AS missing_airports,
                COUNT(*) FILTER (WHERE passengers <= 0)                                   AS non_positive_passengers,
                COUNT(*) FILTER (WHERE mkt_fare <= 0)                                      AS non_positive_fare,
                COUNT(*) FILTER (WHERE mkt_miles_flown <= 0)                              AS non_positive_distance
            FROM raw_airline_data
            WHERE (year, quarter) IN (VALUES {values_clause});
            """,
            scope_params,
        )
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        results = dict(zip(columns, row))

        # Table-wide: cheap, and already guaranteed by the primary key, but
        # kept as a confirmation check for the audit trail.
        cur.execute(
            """
            SELECT COALESCE(SUM(cnt - 1), 0)
            FROM (
                SELECT COUNT(*) AS cnt
                FROM raw_airline_data
                GROUP BY itin_id, mkt_id
                HAVING COUNT(*) > 1
            ) dupes;
            """
        )
        results["duplicate_itin_mkt"] = cur.fetchone()[0]

    results["total_rows"] = total_rows
    return results


def build_report_rows(results: dict, quarters: list) -> list:
    """Turn raw check counts into report rows with pct and pass/fail status."""
    total_rows = results["total_rows"]
    timestamp = datetime.now(timezone.utc).isoformat()
    quarters_label = ",".join(f"{y}Q{q}" for y, q in quarters)

    rows = []
    for check_name, description, category in CHECKS:
        failing = results[check_name]
        pct = (failing / total_rows * 100) if total_rows else 0.0
        status = "FAIL" if failing > 0 else "PASS"
        rows.append(
            {
                "run_timestamp_utc": timestamp,
                "quarters_checked": quarters_label,
                "check_name": check_name,
                "description": description,
                "category": category,
                "failing_rows": failing,
                "total_rows": total_rows,
                "pct_failing": round(pct, 6),
                "status": status,
            }
        )
    return rows


def append_report(rows: list) -> None:
    """Append this run's checks to the report, writing the header only if
    the file is new (or empty), so history accumulates across runs."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_timestamp_utc",
        "quarters_checked",
        "check_name",
        "description",
        "category",
        "failing_rows",
        "total_rows",
        "pct_failing",
        "status",
    ]
    write_header = not (REPORT_FILE.exists() and REPORT_FILE.stat().st_size > 0)
    with open(REPORT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"Validation report updated: {REPORT_FILE}")


def print_summary(rows: list) -> None:
    print(f"\n{'Check':30s} {'Category':13s} {'Failing':>12s} {'% of Total':>12s}  Status")
    print("-" * 80)
    for r in rows:
        print(
            f"{r['check_name']:30s} {r['category']:13s} "
            f"{r['failing_rows']:>12,} {r['pct_failing']:>11.4f}%  {r['status']}"
        )


def mark_validated(cur, quarters: list) -> None:
    for year, quarter in quarters:
        cur.execute(
            """
            INSERT INTO validated_quarters (year, quarter)
            VALUES (%s, %s)
            ON CONFLICT (year, quarter) DO NOTHING;
            """,
            (year, quarter),
        )


def main() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            ensure_tracking_table(cur)
        conn.commit()

        try:
            with conn.cursor() as cur:
                already_validated = get_already_validated(cur)
                available = get_available_quarters(cur)
        except psycopg2.errors.UndefinedTable:
            sys.exit(
                "raw_airline_data does not exist yet - run create_tables.py "
                "and ingest.py before validate.py."
            )

        new_quarters = sorted(available - already_validated)

        if not new_quarters:
            print("No new quarters to validate - everything already checked.")
            return

        print(f"Validating {len(new_quarters)} new quarter(s): "
              f"{', '.join(f'{y}Q{q}' for y, q in new_quarters)}")

        results = run_checks(conn, new_quarters)
        rows = build_report_rows(results, new_quarters)
        append_report(rows)
        print_summary(rows)

        critical_failures = [
            r for r in rows if r["category"] in CRITICAL_CATEGORIES and r["status"] == "FAIL"
        ]
        if critical_failures:
            names = ", ".join(r["check_name"] for r in critical_failures)
            sys.exit(f"\nCritical validation failures: {names}. Halting pipeline.")

        with conn.cursor() as cur:
            mark_validated(cur, new_quarters)
        conn.commit()

        print("\nAll critical checks passed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
