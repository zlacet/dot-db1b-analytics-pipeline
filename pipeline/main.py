"""
main.py

Runs the full ETL pipeline in order:
    ingest.py -> validate.py -> transform.py -> load.py -> generate_reports.py

Each stage is imported and called directly (not run as a subprocess), so
the whole run happens in one Python process and errors propagate cleanly.

ingest.py and validate.py are both incremental (skip already-ingested
files / already-validated quarters), so re-running this after dropping a
new quarterly CSV into data/raw/ only processes what's new in those two
stages. If validate.py finds a critical data quality issue, the pipeline
halts before transform/load touch the data.

transform.py's calculations are only run once here (not once via
transform.py and again inside load.py), since load.load_all() accepts
already-computed DataFrames directly - transform/load always recompute
and reload the full summary tables from the current state of
raw_airline_data, regardless of which quarters were newly ingested.

The final REPORTS stage calls reports/generate_reports.py's run_reports(),
which in turn calls ai_layer/summarize.py's generate_narrative() - Claude
by default (SUMMARY_ENGINE=claude in .env), falling back to the
deterministic template engine only if the Claude call fails - so a single
"python main.py" run refreshes the database AND regenerates
quarterly_report.xlsx + executive_summary.pdf from the latest data. Pass
--skip-reports to stop after load.py instead (e.g. for a bare DB refresh).

Connection settings (same as every other pipeline script) can be
overridden with: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Usage:
    python main.py
    python main.py --skip-reports
    python main.py --report-top 15   # rows per ranking table (default: 10)
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "reports"))

import ingest
import validate
import transform
import load
import generate_reports


def run_stage(name: str, func) -> None:
    """Run one pipeline stage, timing it and turning any sys.exit() raised
    inside it into a clean halt message instead of a bare traceback."""
    print(f"\n{'=' * 70}")
    print(f"STAGE: {name}")
    print("=" * 70)
    start = time.time()
    try:
        func()
    except SystemExit as e:
        message = e.code if isinstance(e.code, str) else f"exited with code {e.code}"
        print(f"\n{name} halted the pipeline: {message}")
        sys.exit(1)
    elapsed = time.time() - start
    print(f"\n{name} finished in {elapsed:.1f}s")


def run_transform_and_load() -> None:
    """Combined transform + load stage: computes the analytics DataFrames
    once, writes the CSV, then loads all three summary tables from that
    same computation."""
    conn = transform.get_connection()
    try:
        route_summary_df, carrier_summary_df, price_variance_df = transform.run_transformations(conn)

        transform.write_cleaned_csv(route_summary_df)
        print(f"Computed {len(carrier_summary_df):,} carrier-quarter rows")
        print(f"Computed {len(price_variance_df):,} price-variance rows")

        load.load_all(conn, route_summary_df, carrier_summary_df, price_variance_df)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full airline price variance ETL pipeline.")
    parser.add_argument(
        "--skip-reports",
        action="store_true",
        help="Stop after load.py instead of also regenerating quarterly_report.xlsx / executive_summary.pdf",
    )
    parser.add_argument(
        "--report-top",
        type=int,
        default=10,
        help="Rows per ranking table in the generated reports (default: 10)",
    )
    args = parser.parse_args()

    pipeline_start = time.time()

    run_stage("INGEST", ingest.main)
    run_stage("VALIDATE", validate.main)
    run_stage("TRANSFORM + LOAD", run_transform_and_load)

    if args.skip_reports:
        print("\n--skip-reports set: leaving quarterly_report.xlsx / executive_summary.pdf untouched")
    else:
        run_stage("REPORTS", lambda: generate_reports.run_reports(top_n=args.report_top))

    total = time.time() - pipeline_start
    print(f"\n{'=' * 70}")
    print(f"Pipeline complete in {total:.1f}s")
    print("=" * 70)


if __name__ == "__main__":
    main()
