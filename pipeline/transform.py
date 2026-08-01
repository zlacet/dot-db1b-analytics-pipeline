"""
transform.py

Reads raw_airline_data, standardizes directional routes into a single
combined route key, and computes the core analytics metrics:
  - passenger-weighted average price
  - price per mile
  - passenger growth % (quarter over quarter, per route)
  - price change % (quarter over quarter, per route)
  - carrier market share %
  - price variance (current vs. previous quarter, per route)

Rows with non-positive passengers, price, or distance (flagged by
validate.py as integrity warnings) are excluded from all aggregations so
they don't skew passenger-weighted averages.

Growth %/change % values are only computed when the prior quarter had at
least MIN_PASSENGERS_FOR_GROWTH passengers. Below that, the percentage is
left NULL instead of reporting a wild swing (e.g. 1 passenger going to
300 the next quarter is not a meaningful "30,000% growth" route - it's
noise from a thin route). Raw passenger/price/price-per-mile figures are
unaffected and always reported, regardless of route size.

Route standardization: routes are defined by exact origin/destination
airport pair (not combined by city market ID - e.g. JFK-LAX and EWR-LAX
are kept as two separate routes). Both directions of a given airport
pair (COS->CMH and CMH->COS) are combined into one canonical label by
keying off LEAST/GREATEST of the two airport codes, so direction never
splits a route in two.

Produces three in-memory result sets via run_transformations():
    route_summary_df, carrier_summary_df, price_variance_df

When run standalone, also writes the route-level result to
data/processed/cleaned_route_data.csv for inspection.

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
CLEANED_ROUTE_FILE = PROCESSED_DIR / "cleaned_route_data.csv"

NUMERIC_ROUTE_COLUMNS = ["passengers", "average_price", "price_per_mile"]
NUMERIC_CARRIER_COLUMNS = ["passengers", "average_price", "price_per_mile"]

# Minimum passengers required in the prior quarter for a growth %/change %
# value to be computed at all; below this, percentages are left NULL since
# they're statistical noise from very thin routes, not meaningful signal.
MIN_PASSENGERS_FOR_GROWTH = 100


def build_clean_base(cur) -> None:
    """Create a session-scoped temp table with non-positive passengers/
    price/distance rows excluded, and a canonical combined route label
    (LEAST/GREATEST of the origin/dest airport codes) that collapses both
    directions of an airport pair into one route."""
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
            LEAST(origin, dest) || '-' || GREATEST(origin, dest) AS route
        FROM raw_airline_data
        WHERE passengers > 0 AND mkt_fare > 0 AND mkt_miles_flown > 0;
        """
    )


def compute_route_quarter_metrics(conn) -> pd.DataFrame:
    """Route + quarter grain: passengers, passenger-weighted average price,
    price per mile. Growth/change % are added afterward in pandas."""
    sql = """
        SELECT
            route,
            year,
            quarter,
            SUM(passengers) AS passengers,
            SUM(mkt_fare * passengers) / SUM(passengers) AS average_price,
            SUM(mkt_fare * passengers) / SUM(mkt_miles_flown * passengers) AS price_per_mile
        FROM clean_base
        GROUP BY route, year, quarter;
    """
    df = pd.read_sql_query(sql, conn)
    df[NUMERIC_ROUTE_COLUMNS] = df[NUMERIC_ROUTE_COLUMNS].astype(float)
    return df


def compute_carrier_quarter_metrics(conn) -> pd.DataFrame:
    """Route + carrier + quarter grain: passengers, passenger-weighted
    average price, price per mile. Market share % is added afterward in
    pandas."""
    sql = """
        SELECT
            route,
            op_carrier AS carrier,
            year,
            quarter,
            SUM(passengers) AS passengers,
            SUM(mkt_fare * passengers) / SUM(passengers) AS average_price,
            SUM(mkt_fare * passengers) / SUM(mkt_miles_flown * passengers) AS price_per_mile
        FROM clean_base
        GROUP BY route, op_carrier, year, quarter;
    """
    df = pd.read_sql_query(sql, conn)
    df[NUMERIC_CARRIER_COLUMNS] = df[NUMERIC_CARRIER_COLUMNS].astype(float)
    return df


def _quarter_sort_key(df: pd.DataFrame) -> pd.Series:
    """Monotonic sort key across year/quarter boundaries, e.g. 2024 Q4 (20244)
    sorts before 2025 Q1 (20251)."""
    return df["year"] * 10 + df["quarter"]


def add_quarter_over_quarter_metrics(route_df: pd.DataFrame) -> pd.DataFrame:
    """Add passenger_growth_pct and price_change_pct, comparing each route's
    quarter to that same route's chronologically previous quarter."""
    df = route_df.copy()
    df["sort_key"] = _quarter_sort_key(df)
    df = df.sort_values(["route", "sort_key"]).reset_index(drop=True)

    grouped = df.groupby("route")
    prev_passengers = grouped["passengers"].shift(1)

    df["passenger_growth_pct"] = grouped["passengers"].pct_change() * 100
    df["price_change_pct"] = grouped["average_price"].pct_change() * 100

    below_threshold = prev_passengers < MIN_PASSENGERS_FOR_GROWTH
    df.loc[below_threshold, "passenger_growth_pct"] = None
    df.loc[below_threshold, "price_change_pct"] = None

    return df.drop(columns="sort_key")


def add_market_share(carrier_df: pd.DataFrame, route_df: pd.DataFrame) -> pd.DataFrame:
    """Add route_share_pct = carrier passengers / total route passengers
    for that route+quarter."""
    df = carrier_df.merge(
        route_df[["route", "year", "quarter", "passengers"]].rename(
            columns={"passengers": "route_total_passengers"}
        ),
        on=["route", "year", "quarter"],
        how="left",
    )
    df["route_share_pct"] = df["passengers"] / df["route_total_passengers"] * 100
    return df.drop(columns="route_total_passengers")


def build_price_variance(route_df: pd.DataFrame) -> pd.DataFrame:
    """Route + quarter grain: current vs previous quarter price and
    price-per-mile, with % change for each."""
    df = route_df.copy()
    df["sort_key"] = _quarter_sort_key(df)
    df = df.sort_values(["route", "sort_key"]).reset_index(drop=True)

    grouped = df.groupby("route")
    df["previous_average_price"] = grouped["average_price"].shift(1)
    df["previous_price_per_mile"] = grouped["price_per_mile"].shift(1)
    prev_passengers = grouped["passengers"].shift(1)

    df["price_change_pct"] = (
        (df["average_price"] - df["previous_average_price"]) / df["previous_average_price"] * 100
    )
    df["price_per_mile_change_pct"] = (
        (df["price_per_mile"] - df["previous_price_per_mile"]) / df["previous_price_per_mile"] * 100
    )

    below_threshold = prev_passengers < MIN_PASSENGERS_FOR_GROWTH
    df.loc[below_threshold, "price_change_pct"] = None
    df.loc[below_threshold, "price_per_mile_change_pct"] = None

    return df.rename(
        columns={
            "average_price": "current_average_price",
            "price_per_mile": "current_price_per_mile",
        }
    )[
        [
            "route",
            "year",
            "quarter",
            "current_average_price",
            "previous_average_price",
            "price_change_pct",
            "current_price_per_mile",
            "previous_price_per_mile",
            "price_per_mile_change_pct",
        ]
    ]


def run_transformations(conn):
    """Build the clean base and compute all three analytics result sets.
    Returns (route_summary_df, carrier_summary_df, price_variance_df)."""
    with conn.cursor() as cur:
        print("Building standardized route base (scans the full raw table)...")
        build_clean_base(cur)
    conn.commit()

    print("Computing route-level metrics...")
    route_raw = compute_route_quarter_metrics(conn)
    route_summary_df = add_quarter_over_quarter_metrics(route_raw)

    print("Computing carrier-level metrics...")
    carrier_raw = compute_carrier_quarter_metrics(conn)
    carrier_summary_df = add_market_share(carrier_raw, route_raw)

    print("Computing price variance metrics...")
    price_variance_df = build_price_variance(route_raw)

    return route_summary_df, carrier_summary_df, price_variance_df


def write_cleaned_csv(route_summary_df: pd.DataFrame) -> None:
    """Write the route-level result to data/processed/cleaned_route_data.csv
    for inspection. Shared by this module's own main() and pipeline/main.py's
    orchestrated run, so the CSV-writing step only lives in one place."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    route_summary_df.to_csv(CLEANED_ROUTE_FILE, index=False)
    print(f"\nWrote {len(route_summary_df):,} route-quarter rows to {CLEANED_ROUTE_FILE}")


def main() -> None:
    conn = get_connection()
    try:
        route_summary_df, carrier_summary_df, price_variance_df = run_transformations(conn)

        write_cleaned_csv(route_summary_df)

        print(f"Computed {len(carrier_summary_df):,} carrier-quarter rows")
        print(f"Computed {len(price_variance_df):,} price-variance rows")

        print("\nSample route_summary rows:")
        print(route_summary_df.head(5).to_string(index=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
