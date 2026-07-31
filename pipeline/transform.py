"""
transform.py

Reads raw_airline_data, standardizes directional markets into a single
combined market key, and computes the core analytics metrics:
  - passenger-weighted average fare
  - fare per mile
  - passenger growth % (quarter over quarter, per market)
  - fare change % (quarter over quarter, per market)
  - carrier market share %
  - fare variance (current vs. previous quarter, per market)

Rows with non-positive passengers, fare, or distance (flagged by
validate.py as integrity warnings) are excluded from all aggregations so
they don't skew passenger-weighted averages.

Growth %/change % values are only computed when the prior quarter had at
least MIN_PASSENGERS_FOR_GROWTH passengers. Below that, the percentage is
left NULL instead of reporting a wild swing (e.g. 1 passenger going to
300 the next quarter is not a meaningful "30,000% growth" market - it's
noise from a thin route). Raw passenger/fare/fare-per-mile figures are
unaffected and always reported, regardless of market size.

Market standardization: markets are defined by exact origin/destination
airport pair (not combined by city market ID - e.g. JFK-LAX and EWR-LAX
are kept as two separate markets). Both directions of a given airport
pair (COS->CMH and CMH->COS) are combined into one canonical label by
keying off LEAST/GREATEST of the two airport codes, so direction never
splits a market in two.

Produces three in-memory result sets via run_transformations():
    market_summary_df, carrier_summary_df, fare_variance_df

When run standalone, also writes the market-level result to
data/processed/cleaned_market_data.csv for inspection.

Connection settings (same as ingest.py/validate.py) can be overridden with:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Usage:
    python transform.py
"""

import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: pandas.\nInstall it with: pip install pandas --break-system-packages")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "database"))
from db import get_connection

PROCESSED_DIR = Path(__file__).resolve().parent.parent / "data" / "processed"
CLEANED_MARKET_FILE = PROCESSED_DIR / "cleaned_market_data.csv"

NUMERIC_MARKET_COLUMNS = ["passengers", "average_fare", "fare_per_mile"]
NUMERIC_CARRIER_COLUMNS = ["passengers", "average_fare"]

# Minimum passengers required in the prior quarter for a growth %/change %
# value to be computed at all; below this, percentages are left NULL since
# they're statistical noise from very thin routes, not meaningful signal.
MIN_PASSENGERS_FOR_GROWTH = 100


def build_clean_base(cur) -> None:
    """Create a session-scoped temp table with non-positive passengers/
    fare/distance rows excluded, and a canonical combined market label
    (LEAST/GREATEST of the origin/dest airport codes) that collapses both
    directions of an airport pair into one market."""
    cur.execute(
        """
        CREATE TEMP TABLE clean_base AS
        SELECT
            year,
            quarter,
            op_carrier,
            passengers,
            mkt_fare,
            mkt_miles_flown,
            LEAST(origin, dest) || '-' || GREATEST(origin, dest) AS market
        FROM raw_airline_data
        WHERE passengers > 0 AND mkt_fare > 0 AND mkt_miles_flown > 0;
        """
    )


def compute_market_quarter_metrics(conn) -> pd.DataFrame:
    """Market + quarter grain: passengers, passenger-weighted average fare,
    fare per mile. Growth/change % are added afterward in pandas."""
    sql = """
        SELECT
            market,
            year,
            quarter,
            SUM(passengers) AS passengers,
            SUM(mkt_fare * passengers) / SUM(passengers) AS average_fare,
            SUM(mkt_fare * passengers) / SUM(mkt_miles_flown * passengers) AS fare_per_mile
        FROM clean_base
        GROUP BY market, year, quarter;
    """
    df = pd.read_sql_query(sql, conn)
    df[NUMERIC_MARKET_COLUMNS] = df[NUMERIC_MARKET_COLUMNS].astype(float)
    return df


def compute_carrier_quarter_metrics(conn) -> pd.DataFrame:
    """Market + carrier + quarter grain: passengers, passenger-weighted
    average fare. Market share % is added afterward in pandas."""
    sql = """
        SELECT
            market,
            op_carrier AS carrier,
            year,
            quarter,
            SUM(passengers) AS passengers,
            SUM(mkt_fare * passengers) / SUM(passengers) AS average_fare
        FROM clean_base
        GROUP BY market, op_carrier, year, quarter;
    """
    df = pd.read_sql_query(sql, conn)
    df[NUMERIC_CARRIER_COLUMNS] = df[NUMERIC_CARRIER_COLUMNS].astype(float)
    return df


def _quarter_sort_key(df: pd.DataFrame) -> pd.Series:
    """Monotonic sort key across year/quarter boundaries, e.g. 2024 Q4 (20244)
    sorts before 2025 Q1 (20251)."""
    return df["year"] * 10 + df["quarter"]


def add_quarter_over_quarter_metrics(market_df: pd.DataFrame) -> pd.DataFrame:
    """Add passenger_growth_pct and fare_change_pct, comparing each market's
    quarter to that same market's chronologically previous quarter."""
    df = market_df.copy()
    df["sort_key"] = _quarter_sort_key(df)
    df = df.sort_values(["market", "sort_key"]).reset_index(drop=True)

    grouped = df.groupby("market")
    prev_passengers = grouped["passengers"].shift(1)

    df["passenger_growth_pct"] = grouped["passengers"].pct_change() * 100
    df["fare_change_pct"] = grouped["average_fare"].pct_change() * 100

    below_threshold = prev_passengers < MIN_PASSENGERS_FOR_GROWTH
    df.loc[below_threshold, "passenger_growth_pct"] = None
    df.loc[below_threshold, "fare_change_pct"] = None

    return df.drop(columns="sort_key")


def add_market_share(carrier_df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    """Add market_share_pct = carrier passengers / total market passengers
    for that market+quarter."""
    df = carrier_df.merge(
        market_df[["market", "year", "quarter", "passengers"]].rename(
            columns={"passengers": "market_total_passengers"}
        ),
        on=["market", "year", "quarter"],
        how="left",
    )
    df["market_share_pct"] = df["passengers"] / df["market_total_passengers"] * 100
    return df.drop(columns="market_total_passengers")


def build_fare_variance(market_df: pd.DataFrame) -> pd.DataFrame:
    """Market + quarter grain: current vs previous quarter fare and
    fare-per-mile, with % change for each."""
    df = market_df.copy()
    df["sort_key"] = _quarter_sort_key(df)
    df = df.sort_values(["market", "sort_key"]).reset_index(drop=True)

    grouped = df.groupby("market")
    df["previous_average_fare"] = grouped["average_fare"].shift(1)
    df["previous_fare_per_mile"] = grouped["fare_per_mile"].shift(1)
    prev_passengers = grouped["passengers"].shift(1)

    df["fare_change_pct"] = (
        (df["average_fare"] - df["previous_average_fare"]) / df["previous_average_fare"] * 100
    )
    df["fare_per_mile_change_pct"] = (
        (df["fare_per_mile"] - df["previous_fare_per_mile"]) / df["previous_fare_per_mile"] * 100
    )

    below_threshold = prev_passengers < MIN_PASSENGERS_FOR_GROWTH
    df.loc[below_threshold, "fare_change_pct"] = None
    df.loc[below_threshold, "fare_per_mile_change_pct"] = None

    return df.rename(
        columns={
            "average_fare": "current_average_fare",
            "fare_per_mile": "current_fare_per_mile",
        }
    )[
        [
            "market",
            "year",
            "quarter",
            "current_average_fare",
            "previous_average_fare",
            "fare_change_pct",
            "current_fare_per_mile",
            "previous_fare_per_mile",
            "fare_per_mile_change_pct",
        ]
    ]


def run_transformations(conn):
    """Build the clean base and compute all three analytics result sets.
    Returns (market_summary_df, carrier_summary_df, fare_variance_df)."""
    with conn.cursor() as cur:
        print("Building standardized market base (scans the full raw table)...")
        build_clean_base(cur)
    conn.commit()

    print("Computing market-level metrics...")
    market_raw = compute_market_quarter_metrics(conn)
    market_summary_df = add_quarter_over_quarter_metrics(market_raw)

    print("Computing carrier-level metrics...")
    carrier_raw = compute_carrier_quarter_metrics(conn)
    carrier_summary_df = add_market_share(carrier_raw, market_raw)

    print("Computing fare variance metrics...")
    fare_variance_df = build_fare_variance(market_raw)

    return market_summary_df, carrier_summary_df, fare_variance_df


def write_cleaned_csv(market_summary_df: pd.DataFrame) -> None:
    """Write the market-level result to data/processed/cleaned_market_data.csv
    for inspection. Shared by this module's own main() and pipeline/main.py's
    orchestrated run, so the CSV-writing step only lives in one place."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    market_summary_df.to_csv(CLEANED_MARKET_FILE, index=False)
    print(f"\nWrote {len(market_summary_df):,} market-quarter rows to {CLEANED_MARKET_FILE}")


def main() -> None:
    conn = get_connection()
    try:
        market_summary_df, carrier_summary_df, fare_variance_df = run_transformations(conn)

        write_cleaned_csv(market_summary_df)

        print(f"Computed {len(carrier_summary_df):,} carrier-quarter rows")
        print(f"Computed {len(fare_variance_df):,} fare-variance rows")

        print("\nSample market_summary rows:")
        print(market_summary_df.head(5).to_string(index=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
