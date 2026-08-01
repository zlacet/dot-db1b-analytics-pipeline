"""
summarize.py

Generates a narrative executive summary from the route_summary,
carrier_summary, and price_variance_summary tables.

Two engines produce the narrative from those same facts:

  template  - deterministic Python (describe_growth/describe_price_change
              template phrases into sentences). No API key needed, always
              works, wording is repetitive across runs.
  claude    - makes one Claude API call per section (13 of SECTION_ORDER's
              14 entries, each returning that section's blurb as plain
              text), plus 8 more calls for "Overall Consensus" (opening +
              Quarterly Trends paragraph + Insight/Takeaway pairs for
              Carrier Performance, Price Trends, and Route Performance -
              see _build_consensus_claude()) - 21 external API calls
              total. Requires ANTHROPIC_API_KEY and the `anthropic`
              package. More natural/varied writing; each call fails
              independently and falls back to the template engine's
              version if it does.

Both engines return a dict of {section_title: blurb} - one short blurb per
table in the PDF report (see SECTION_ORDER), plus "Overall Consensus" (a
dict of {opening, Quarterly Trends, Carrier Performance, Price Trends,
Route Performance} - opening/Quarterly Trends are plain strings, while
Carrier Performance/Price Trends/Route Performance are each a dict of
{insight, takeaway}, since that section is now a short overview followed
by four subsections on its own page, three of which are split into a
factual insight and a separate actionable takeaway) - so
reports/generate_reports.py can place each blurb directly under its
matching table instead of one big block of text. Both engines cover the
same ground: the whole-dataset quarterly trend first, then top routes by
volume, best/worst passenger growth, best/worst price movement
(company-revenue framing: a price increase is "best"), best/worst price
efficiency (price per mile relative to distance-band peers), and top
carriers by passenger volume, price efficiency, price-per-mile growth, and
market share. "Overall Consensus" describes the whole-population aggregate
pattern (direction/dispersion, no specific route/carrier names) rather
than repeating the standouts already named in the sections above it.

Engine selection: --engine flag, falling back to the SUMMARY_ENGINE
environment variable, falling back to "template" if neither is set.

Connection settings (same as the rest of the pipeline) can be overridden
with: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Reads a .env file (project root) if present, via python-dotenv, so
ANTHROPIC_API_KEY / PG* variables can live there instead of your shell.
A real .env should never be committed - see .env.example for the template
and .gitignore for the exclusion. Values already set in your actual shell
environment take precedence over the .env file.

Usage:
    python summarize.py                          # template engine, top 5
    python summarize.py --engine claude           # Claude-written prose
    python summarize.py --top 10                  # top 10 routes instead of 5
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed - fine as long as vars are set another way

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: pandas.\nInstall it with: pip install pandas --break-system-packages")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "database"))
from db import get_connection

# Growth/price-change % thresholds used to pick descriptive language.
STRONG_GROWTH_PCT = 10.0
DECLINE_PCT = -5.0
STABLE_PRICE_BAND_PCT = 3.0
PRICE_INCREASE_PCT = 10.0

# Minimum total passengers a carrier needs (that quarter) to be eligible for
# the price-efficiency / average-market-share best/worst rankings. Without
# this, a carrier with a single low-volume appearance can average out to a
# misleading 100% market share and show up as a "top" performer. Doesn't
# affect the full Carrier Summary listing or the by-passengers rankings,
# where showing genuinely small carriers is the point.
MIN_CARRIER_PASSENGERS = 100

# Minimum current-quarter passengers a route needs to be eligible for the
# best/worst passenger-growth, price-movement, and price-efficiency rankings.
# Separate from (and higher than) the 100-passenger prior-quarter floor
# transform.py already uses to decide whether growth/change % gets computed
# at all - that check only looks at the prior quarter, so a route can go
# from ~110 to 900 passengers and still post an eye-catching (but not very
# meaningful) triple-digit percentage swing. Doesn't affect "Top Routes by
# Volume", which is inherently high-volume already.
MIN_ROUTE_PASSENGERS = 5000

# Distance bands used to normalize price-per-mile comparisons across routes
# of very different lengths (short routes are always more expensive per
# mile than long ones due to fixed costs, so comparing raw price-per-mile
# across all routes would just re-show that pattern instead of surfacing
# real pricing outliers). Distance itself isn't a stored column - it's
# derived as average_price / price_per_mile (both already in route_summary),
# which works out to the passenger-weighted average miles for the route.
DISTANCE_BAND_EDGES = [0, 500, 1500, float("inf")]
DISTANCE_BAND_LABELS = ["Short-haul (<500mi)", "Medium-haul (500-1500mi)", "Long-haul (1500mi+)"]


def get_quarter_range(conn) -> tuple:
    """Return (first_year, first_quarter, last_year, last_quarter) present
    in route_summary, used to phrase the "From ... through ..." lead-in."""
    sql = """
        SELECT MIN(year * 10 + quarter) AS first_key,
               MAX(year * 10 + quarter) AS last_key
        FROM route_summary;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        first_key, last_key = cur.fetchone()
    return (first_key // 10, first_key % 10, last_key // 10, last_key % 10)


def get_quarterly_trend(conn) -> pd.DataFrame:
    """Whole-dataset rollup: one row per (year, quarter) across every
    quarter in route_summary - not per-route, every route combined.
    Same KPI set as the original Tableau Executive Overview page: total
    passengers, total routes, total carriers, passenger-weighted average
    price, and passenger-weighted price per mile. total_carriers comes from
    a separate carrier_summary rollup merged in on (year, quarter), since
    carrier isn't a route_summary column. passenger_growth_pct/
    price_change_pct are quarter-over-quarter change computed in pandas
    afterward (first quarter has no prior quarter, so its value is NaN)."""
    sql = """
        SELECT
            year,
            quarter,
            SUM(passengers) AS total_passengers,
            COUNT(DISTINCT route) AS total_routes,
            SUM(average_price * passengers) / SUM(passengers) AS average_price,
            SUM(price_per_mile * passengers) / SUM(passengers) AS price_per_mile
        FROM route_summary
        GROUP BY year, quarter
        ORDER BY year, quarter;
    """
    df = pd.read_sql_query(sql, conn)

    carriers_sql = """
        SELECT year, quarter, COUNT(DISTINCT carrier) AS total_carriers
        FROM carrier_summary
        GROUP BY year, quarter;
    """
    carriers_df = pd.read_sql_query(carriers_sql, conn)
    df = df.merge(carriers_df, on=["year", "quarter"], how="left")

    df["passenger_growth_pct"] = df["total_passengers"].pct_change() * 100
    df["price_change_pct"] = df["average_price"].pct_change() * 100
    return df


def get_top_routes(conn, latest_year: int, latest_quarter: int, top_n: int = None) -> pd.DataFrame:
    """Top N routes in the latest quarter by passenger volume, joined with
    that route's price_variance_summary row for price_change_pct. Pass
    top_n=None for every route with no limit (used for the unlimited xlsx
    sheets; the narrative engines always pass an explicit top_n)."""
    limit_clause = "LIMIT %s" if top_n is not None else ""
    params = (latest_year, latest_quarter) if top_n is None else (latest_year, latest_quarter, top_n)
    sql = f"""
        SELECT
            r.route,
            r.passengers,
            r.passenger_growth_pct,
            r.average_price,
            r.price_per_mile,
            f.price_change_pct,
            f.price_per_mile_change_pct
        FROM route_summary r
        LEFT JOIN price_variance_summary f
            ON f.route = r.route AND f.year = r.year AND f.quarter = r.quarter
        WHERE r.year = %s AND r.quarter = %s
        ORDER BY r.passengers DESC
        {limit_clause};
    """
    return pd.read_sql_query(sql, conn, params=params)


def get_leading_carrier(conn, route: str, year: int, quarter: int):
    """Return (carrier, route_share_pct) for the top carrier in a given
    route + quarter, or (None, None) if no carrier rows exist."""
    sql = """
        SELECT carrier, route_share_pct
        FROM carrier_summary
        WHERE route = %s AND year = %s AND quarter = %s
        ORDER BY route_share_pct DESC
        LIMIT 1;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (route, year, quarter))
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


# Columns get_ranked_routes()/get_ranked_carriers() are allowed to sort by.
# Whitelisted (not user input) since the column name is interpolated
# directly into the SQL string - only ever called with these fixed values.
_RANKABLE_ROUTE_COLUMNS = {"passenger_growth_pct": "r", "price_change_pct": "f"}
_RANKABLE_CARRIER_COLUMNS = {"total_passengers", "avg_price", "avg_price_per_mile", "avg_route_share_pct"}


def get_ranked_routes(
    conn, year: int, quarter: int, order_by: str, ascending: bool, top_n: int = None,
    min_passengers: int = None,
) -> pd.DataFrame:
    """Same route_summary + price_variance_summary join/shape as
    get_top_routes(), but ranked by passenger_growth_pct or price_change_pct
    instead of passenger volume, and excluding routes with no value for
    that column (thin routes with no prior-quarter comparison). Pass
    top_n=None for every qualifying route with no limit. Pass
    min_passengers to also require the route's current-quarter passenger
    count meet a floor - separate from (and independent of) the
    prior-quarter floor transform.py already applies before computing
    growth/change % at all, so a route that jumped from a tiny prior
    quarter to a still-small current quarter can't post an eye-catching but
    not very meaningful percentage swing."""
    if order_by not in _RANKABLE_ROUTE_COLUMNS:
        raise ValueError(f"order_by must be one of {sorted(_RANKABLE_ROUTE_COLUMNS)}")
    table_alias = _RANKABLE_ROUTE_COLUMNS[order_by]
    direction = "ASC" if ascending else "DESC"
    passenger_filter = "AND r.passengers >= %s" if min_passengers is not None else ""
    limit_clause = "LIMIT %s" if top_n is not None else ""
    params = [year, quarter]
    if min_passengers is not None:
        params.append(min_passengers)
    if top_n is not None:
        params.append(top_n)
    sql = f"""
        SELECT
            r.route,
            r.passengers,
            r.passenger_growth_pct,
            r.average_price,
            r.price_per_mile,
            f.price_change_pct,
            f.price_per_mile_change_pct
        FROM route_summary r
        LEFT JOIN price_variance_summary f
            ON f.route = r.route AND f.year = r.year AND f.quarter = r.quarter
        WHERE r.year = %s AND r.quarter = %s
          AND {table_alias}.{order_by} IS NOT NULL
          {passenger_filter}
        ORDER BY {table_alias}.{order_by} {direction}
        {limit_clause};
    """
    return pd.read_sql_query(sql, conn, params=tuple(params))


def add_price_multiplier(df: pd.DataFrame) -> pd.DataFrame:
    """Add distance, distance_band, and price_multiplier columns to a
    routes dataframe (anything with average_price and price_per_mile
    columns, e.g. get_top_routes()'s output).

    distance is derived (average_price / price_per_mile both already carry a
    passenger-weighted average, so the ratio works out to passenger-
    weighted average miles) rather than stored, since route_summary has no
    raw distance column.

    price_multiplier = this route's price_per_mile divided by the
    average price_per_mile of every other route in the same distance band.
    An index above 1.0 means the route is priced above its distance peers
    (from the company's revenue perspective, a positive); below 1.0 means
    it's priced below them. This exists so "top/bottom price-per-mile"
    rankings surface real pricing outliers instead of just re-showing that
    short routes always cost more per mile than long ones - see the
    Price-Per-Mile Outliers chart in the Tableau dashboard for the same
    logic applied there.

    Shared between get_ranked_route_efficiency() (narrative engines) and
    reports/generate_reports.py's _pdf_sections() (PDF/xlsx tables), so
    both can never disagree on which routes are efficiency outliers."""
    df = df.copy()
    df["distance"] = df["average_price"] / df["price_per_mile"]
    df["distance_band"] = pd.cut(df["distance"], bins=DISTANCE_BAND_EDGES, labels=DISTANCE_BAND_LABELS)
    band_avg = df.groupby("distance_band", observed=True)["price_per_mile"].transform("mean")
    df["price_multiplier"] = df["price_per_mile"] / band_avg
    return df


def get_ranked_route_efficiency(
    conn, year: int, quarter: int, ascending: bool, top_n: int = None, min_passengers: int = None,
) -> pd.DataFrame:
    """Every route in the given quarter (reusing get_top_routes()'s query),
    with add_price_multiplier() applied, then ranked by
    price_multiplier and sliced to top_n. Pass min_passengers to apply
    the same route-size floor as the growth/price-movement rankings."""
    df = get_top_routes(conn, year, quarter, top_n=None)
    if min_passengers is not None:
        df = df[df["passengers"] >= min_passengers]
    df = df.dropna(subset=["average_price", "price_per_mile"])
    df = add_price_multiplier(df)
    df = df.sort_values("price_multiplier", ascending=ascending)
    if top_n is not None:
        df = df.head(top_n)
    return df


def get_carrier_quarterly_rollup(conn) -> pd.DataFrame:
    """Carrier + quarter rollup (every route for that carrier combined)
    across every quarter in carrier_summary: total passengers and
    passenger-weighted average price per mile. carrier_summary itself is
    route + carrier + quarter grain with no growth tracking of its own
    (unlike route_summary, which already has passenger_growth_pct/
    price_change_pct from transform.py), so this exists to compute
    carrier-level quarter-over-quarter price-per-mile growth in pandas
    afterward - see get_ranked_carrier_price_per_mile_growth()."""
    sql = """
        SELECT
            carrier,
            year,
            quarter,
            SUM(passengers) AS total_passengers,
            SUM(price_per_mile * passengers) / SUM(passengers) AS avg_price_per_mile
        FROM carrier_summary
        GROUP BY carrier, year, quarter;
    """
    df = pd.read_sql_query(sql, conn)
    df["sort_key"] = df["year"] * 10 + df["quarter"]
    df = df.sort_values(["carrier", "sort_key"]).reset_index(drop=True)
    df["price_per_mile_growth_pct"] = df.groupby("carrier")["avg_price_per_mile"].pct_change() * 100
    return df.drop(columns="sort_key")


def get_ranked_carrier_price_per_mile_growth(
    conn, year: int, quarter: int, ascending: bool = False, top_n: int = None, min_passengers: int = None,
) -> pd.DataFrame:
    """One quarter's slice of get_carrier_quarterly_rollup(), ranked by
    price_per_mile_growth_pct (carriers with no prior quarter to compare
    against are excluded). Pass top_n=None for every qualifying carrier
    with no limit (used for the Overall Consensus aggregate stats); the
    "Top Carriers by Price-Per-Mile Growth" section always passes an
    explicit top_n."""
    df = get_carrier_quarterly_rollup(conn)
    current = df[(df["year"] == year) & (df["quarter"] == quarter)].dropna(subset=["price_per_mile_growth_pct"])
    if min_passengers is not None:
        current = current[current["total_passengers"] >= min_passengers]
    current = current.sort_values("price_per_mile_growth_pct", ascending=ascending)
    if top_n is not None:
        current = current.head(top_n)
    return current.reset_index(drop=True)[
        ["carrier", "total_passengers", "avg_price_per_mile", "price_per_mile_growth_pct"]
    ]


def get_ranked_carriers(
    conn, year: int, quarter: int, order_by: str, ascending: bool, top_n: int = None,
    min_passengers: int = None,
) -> pd.DataFrame:
    """Carrier-level rollup (same shape as generate_reports.py's former
    fetch_carrier_performance): total passengers, passenger-weighted average
    price, passenger-weighted average price per mile, and average market
    share per carrier, ranked by whichever of those four columns is
    requested. Pass top_n=None for every carrier with no limit. Pass
    min_passengers to exclude carriers whose total passengers that quarter
    fall below the threshold - used for the avg_price_per_mile/
    avg_route_share_pct rankings so a carrier with a single low-volume
    appearance can't average out to a misleading 100% share and rank as a
    "top" performer."""
    if order_by not in _RANKABLE_CARRIER_COLUMNS:
        raise ValueError(f"order_by must be one of {sorted(_RANKABLE_CARRIER_COLUMNS)}")
    direction = "ASC" if ascending else "DESC"
    having_clause = "HAVING SUM(passengers) >= %s" if min_passengers is not None else ""
    limit_clause = "LIMIT %s" if top_n is not None else ""
    params = [year, quarter]
    if min_passengers is not None:
        params.append(min_passengers)
    if top_n is not None:
        params.append(top_n)
    sql = f"""
        SELECT carrier,
               SUM(passengers) AS total_passengers,
               SUM(average_price * passengers) / SUM(passengers) AS avg_price,
               SUM(price_per_mile * passengers) / SUM(passengers) AS avg_price_per_mile,
               AVG(route_share_pct) AS avg_route_share_pct
        FROM carrier_summary
        WHERE year = %s AND quarter = %s
        GROUP BY carrier
        {having_clause}
        ORDER BY {order_by} {direction}
        {limit_clause};
    """
    return pd.read_sql_query(sql, conn, params=tuple(params))


def describe_growth(pct) -> str:
    if pct is None or pd.isna(pct):
        return "passenger demand on record"
    if pct >= STRONG_GROWTH_PCT:
        return f"strong passenger growth (+{pct:.1f}%)"
    if pct <= DECLINE_PCT:
        return f"declining passenger demand ({pct:.1f}%)"
    return f"steady passenger demand ({pct:+.1f}%)"


def describe_price_change(pct) -> str:
    if pct is None or pd.isna(pct):
        return "no prior-quarter price data"
    if abs(pct) <= STABLE_PRICE_BAND_PCT:
        return "stable price levels"
    if pct >= PRICE_INCREASE_PCT:
        return f"a notable price increase (+{pct:.1f}%)"
    if pct > 0:
        return f"a modest price increase (+{pct:.1f}%)"
    return f"lower prices ({pct:.1f}%)"


def describe_price_per_mile_change(pct) -> str:
    """Same thresholds as describe_price_change, worded around per-mile
    pricing efficiency rather than the flat average price."""
    if pct is None or pd.isna(pct):
        return None
    if abs(pct) <= STABLE_PRICE_BAND_PCT:
        return "price-per-mile efficiency held steady"
    if pct >= PRICE_INCREASE_PCT:
        return f"price-per-mile rose sharply (+{pct:.1f}%)"
    if pct > 0:
        return f"price-per-mile edged up (+{pct:.1f}%)"
    return f"price-per-mile improved ({pct:.1f}%)"


def build_route_sentence(row, carrier, route_share) -> str:
    growth_phrase = describe_growth(row["passenger_growth_pct"])
    price_phrase = describe_price_change(row["price_change_pct"])
    price_per_mile_phrase = describe_price_per_mile_change(row.get("price_per_mile_change_pct"))

    sentence = f"{row['route']} showed {growth_phrase} while recording {price_phrase}."
    if price_per_mile_phrase:
        sentence += f" {price_per_mile_phrase.capitalize()}."
    if carrier:
        sentence += f" {carrier} led the route with {route_share:.1f}% share."
    return sentence


def _build_route_facts(conn, df: pd.DataFrame, year: int, quarter: int) -> list:
    """Turn a route_summary/price_variance_summary query result into the
    per-route fact dicts used throughout this module, adding each route's
    leading carrier. Shared by every "list of routes" section in
    gather_facts() so they all carry the same fields."""
    routes = []
    for _, row in df.iterrows():
        carrier, route_share = get_leading_carrier(conn, row["route"], year, quarter)
        routes.append(
            {
                "route": row["route"],
                "passengers": row["passengers"],
                "passenger_growth_pct": row["passenger_growth_pct"],
                "average_price": row["average_price"],
                "price_per_mile": row["price_per_mile"],
                "price_change_pct": row["price_change_pct"],
                "price_per_mile_change_pct": row["price_per_mile_change_pct"],
                "leading_carrier": carrier,
                "leading_carrier_share_pct": route_share,
            }
        )
    return routes


def _build_route_efficiency_facts(df: pd.DataFrame) -> list:
    """Turn a get_ranked_route_efficiency() result into fact dicts - a
    different, lighter shape than _build_route_facts() since these rows
    carry distance/price_multiplier instead of growth/price-change
    fields, and don't need a leading-carrier lookup."""
    facts = []
    for _, row in df.iterrows():
        facts.append(
            {
                "route": row["route"],
                "passengers": row["passengers"],
                "price_per_mile": row["price_per_mile"],
                "distance": row["distance"],
                "distance_band": str(row["distance_band"]),
                "price_multiplier": row["price_multiplier"],
            }
        )
    return facts


# Fixed top-N for the best/worst route sections below, per the agreed
# design (separate from `top_n`, which only controls the original
# top-by-volume "routes" list used by both engines today).
BEST_WORST_TOP_N = 3

# Best/Worst Price Efficiency show more rows than the growth/price-movement
# sections - kept separate from BEST_WORST_TOP_N.
ROUTE_EFFICIENCY_TOP_N = 5

# Same idea, but for the carrier best/worst sections (by passengers, price
# efficiency, and price-per-mile growth) - kept separate from
# BEST_WORST_TOP_N so the carrier tables can show more rows without
# affecting the route growth/price/efficiency tables.
CARRIER_BEST_WORST_TOP_N = 5

# Top Carriers by Market Share shows more rows than the other carrier
# sections - kept separate from CARRIER_BEST_WORST_TOP_N.
CARRIER_SHARE_TOP_N = 10

# Both narrative engines return a dict of {section_title: blurb}, one entry
# per table in the PDF report plus "Overall Consensus" (the standalone wrap-up
# paragraph, not tied to any one table). Keys match the table titles used in
# reports/generate_reports.py's DOMAIN_GROUPS exactly, so a blurb can be
# looked up and placed directly under its matching table.
SECTION_ORDER = [
    "Quarterly Trends",
    "Top Routes by Volume",
    "Best Passenger Growth",
    "Worst Passenger Growth",
    "Best Price Movement",
    "Worst Price Movement",
    "Best Price Efficiency",
    "Worst Price Efficiency",
    "Top Carriers by Passengers",
    "Bottom Carriers by Passengers",
    "Top Carriers by Price Efficiency",
    "Top Carriers by Price-Per-Mile Growth",
    "Top Carriers by Market Share",
    "Overall Consensus",
]


def gather_facts(conn, top_n: int = 5) -> dict:
    """Pull the raw numbers both the template engine and the Claude engine
    build their narrative from, so the two engines never disagree on facts
    - only on how the facts are worded.

    "quarterly_trend" is the whole dataset (every route combined) rolled
    up to one row per quarter, read first by both engines before anything
    route/carrier-specific."""
    first_year, first_quarter, last_year, last_quarter = get_quarter_range(conn)

    quarterly_trend = get_quarterly_trend(conn).to_dict("records")

    top_routes_df = get_top_routes(conn, last_year, last_quarter, top_n)
    routes = _build_route_facts(conn, top_routes_df, last_year, last_quarter)

    best_growth_df = get_ranked_routes(
        conn, last_year, last_quarter, "passenger_growth_pct", ascending=False, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_ROUTE_PASSENGERS,
    )
    worst_growth_df = get_ranked_routes(
        conn, last_year, last_quarter, "passenger_growth_pct", ascending=True, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_ROUTE_PASSENGERS,
    )
    # Company-revenue framing: prices UP is "best" (more revenue per
    # passenger), prices DOWN is "worst" (less revenue per passenger).
    best_price_df = get_ranked_routes(
        conn, last_year, last_quarter, "price_change_pct", ascending=False, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_ROUTE_PASSENGERS,
    )
    worst_price_df = get_ranked_routes(
        conn, last_year, last_quarter, "price_change_pct", ascending=True, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_ROUTE_PASSENGERS,
    )

    best_growth_routes = _build_route_facts(conn, best_growth_df, last_year, last_quarter)
    worst_growth_routes = _build_route_facts(conn, worst_growth_df, last_year, last_quarter)
    best_price_routes = _build_route_facts(conn, best_price_df, last_year, last_quarter)
    worst_price_routes = _build_route_facts(conn, worst_price_df, last_year, last_quarter)

    # Price efficiency: priced above/below distance-band peers. Same
    # company-revenue framing as price movement - "best" is priced above
    # peers (more revenue per mile than routes of similar distance). Shows
    # more rows than growth/price movement (ROUTE_EFFICIENCY_TOP_N).
    best_efficiency_df = get_ranked_route_efficiency(
        conn, last_year, last_quarter, ascending=False, top_n=ROUTE_EFFICIENCY_TOP_N,
        min_passengers=MIN_ROUTE_PASSENGERS,
    )
    worst_efficiency_df = get_ranked_route_efficiency(
        conn, last_year, last_quarter, ascending=True, top_n=ROUTE_EFFICIENCY_TOP_N,
        min_passengers=MIN_ROUTE_PASSENGERS,
    )
    best_price_efficiency_routes = _build_route_efficiency_facts(best_efficiency_df)
    worst_price_efficiency_routes = _build_route_efficiency_facts(worst_efficiency_df)

    best_carrier_passengers = get_ranked_carriers(
        conn, last_year, last_quarter, "total_passengers", ascending=False, top_n=CARRIER_BEST_WORST_TOP_N
    ).to_dict("records")
    worst_carrier_passengers = get_ranked_carriers(
        conn, last_year, last_quarter, "total_passengers", ascending=True, top_n=CARRIER_BEST_WORST_TOP_N
    ).to_dict("records")
    best_carrier_efficiency = get_ranked_carriers(
        conn, last_year, last_quarter, "avg_price_per_mile", ascending=False, top_n=CARRIER_BEST_WORST_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")
    carrier_price_per_mile_growth = get_ranked_carrier_price_per_mile_growth(
        conn, last_year, last_quarter, ascending=False, top_n=CARRIER_BEST_WORST_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")
    # Shows more rows than the other carrier sections (CARRIER_SHARE_TOP_N).
    best_carrier_share = get_ranked_carriers(
        conn, last_year, last_quarter, "avg_route_share_pct", ascending=False, top_n=CARRIER_SHARE_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")

    # Whole-population (not just the best/worst-N slices above) facts for
    # the Overall Consensus section - describes the overall shape of the
    # market (direction/dispersion, no names) rather than repeating the
    # standout routes/carriers already covered in their own sections.
    all_routes_df = get_top_routes(conn, last_year, last_quarter, top_n=None)
    qualifying_all_routes = all_routes_df[all_routes_df["passengers"] >= MIN_ROUTE_PASSENGERS]
    growth_valid = qualifying_all_routes["passenger_growth_pct"].dropna()
    price_valid = qualifying_all_routes["price_change_pct"].dropna()
    efficiency_all_routes = add_price_multiplier(
        qualifying_all_routes.dropna(subset=["average_price", "price_per_mile"])
    )

    all_carriers_df = get_ranked_carriers(
        conn, last_year, last_quarter, "total_passengers", ascending=False, top_n=None
    )
    qualifying_all_carriers = all_carriers_df[all_carriers_df["total_passengers"] >= MIN_CARRIER_PASSENGERS]

    carrier_growth_all = get_ranked_carrier_price_per_mile_growth(
        conn, last_year, last_quarter, top_n=None, min_passengers=MIN_CARRIER_PASSENGERS
    )

    aggregate = _compute_aggregate_facts(
        qualifying_all_routes, growth_valid, price_valid, efficiency_all_routes,
        qualifying_all_carriers, carrier_growth_all,
    )

    return {
        "first_year": first_year,
        "first_quarter": first_quarter,
        "last_year": last_year,
        "last_quarter": last_quarter,
        "quarterly_trend": quarterly_trend,
        "routes": routes,
        "best_growth_routes": best_growth_routes,
        "worst_growth_routes": worst_growth_routes,
        "best_price_routes": best_price_routes,
        "worst_price_routes": worst_price_routes,
        "best_price_efficiency_routes": best_price_efficiency_routes,
        "worst_price_efficiency_routes": worst_price_efficiency_routes,
        "best_carrier_passengers": best_carrier_passengers,
        "worst_carrier_passengers": worst_carrier_passengers,
        "best_carrier_efficiency": best_carrier_efficiency,
        "carrier_price_per_mile_growth": carrier_price_per_mile_growth,
        "best_carrier_share": best_carrier_share,
        "aggregate": aggregate,
    }


def _compute_aggregate_facts(
    qualifying_routes: pd.DataFrame, growth_valid: pd.Series, price_valid: pd.Series,
    efficiency_routes: pd.DataFrame, qualifying_carriers: pd.DataFrame, carrier_growth: pd.DataFrame,
) -> dict:
    """Whole-population aggregate stats (direction/dispersion across the
    entire qualifying set, not any one named route or carrier) used to
    write the Overall Consensus section's four prose paragraphs. Computed
    from the full qualifying population for each metric (not the
    best/worst-N slices used elsewhere in this module), so the consensus
    describes the overall shape of the market rather than repeating the
    standout names already covered in their own sections."""

    def pct_positive(series: pd.Series):
        return None if series.empty else float((series > 0).mean() * 100)

    def pct_negative(series: pd.Series):
        return None if series.empty else float((series < 0).mean() * 100)

    def median_or_none(series: pd.Series):
        return None if series.empty else float(series.median())

    top_carrier_share_pct = None
    top3_carrier_share_pct = None
    if not qualifying_carriers.empty:
        sorted_pax = qualifying_carriers["total_passengers"].sort_values(ascending=False)
        total_pax = sorted_pax.sum()
        if total_pax > 0:
            top_carrier_share_pct = float(sorted_pax.iloc[0] / total_pax * 100)
            top3_carrier_share_pct = float(sorted_pax.head(3).sum() / total_pax * 100)

    pct_priced_above_peers = None
    mean_efficiency_index = None
    if not efficiency_routes.empty:
        pct_priced_above_peers = float((efficiency_routes["price_multiplier"] > 1).mean() * 100)
        mean_efficiency_index = float(efficiency_routes["price_multiplier"].mean())

    pct_carriers_growing = None
    if not carrier_growth.empty:
        pct_carriers_growing = pct_positive(carrier_growth["price_per_mile_growth_pct"])

    pct_carriers_above_avg_fpm = None
    if not qualifying_carriers.empty:
        avg_fpm = qualifying_carriers["avg_price_per_mile"].mean()
        pct_carriers_above_avg_fpm = float((qualifying_carriers["avg_price_per_mile"] > avg_fpm).mean() * 100)

    return {
        "num_qualifying_routes": int(len(qualifying_routes)),
        "pct_routes_growing_passengers": pct_positive(growth_valid),
        "pct_routes_declining_passengers": pct_negative(growth_valid),
        "median_passenger_growth_pct": median_or_none(growth_valid),
        "pct_routes_price_up": pct_positive(price_valid),
        "pct_routes_price_down": pct_negative(price_valid),
        "median_price_change_pct": median_or_none(price_valid),
        "pct_routes_priced_above_peers": pct_priced_above_peers,
        "mean_price_multiplier": mean_efficiency_index,
        "num_qualifying_carriers": int(len(qualifying_carriers)),
        "top_carrier_share_pct": top_carrier_share_pct,
        "top3_carrier_share_pct": top3_carrier_share_pct,
        "pct_carriers_price_per_mile_growing": pct_carriers_growing,
        "pct_carriers_priced_above_avg_price_per_mile": pct_carriers_above_avg_fpm,
    }


def _route_sentence_from_fact(m: dict) -> str:
    """Adapter so build_route_sentence() (which expects a plain dict with
    just the fields it reads) can be reused for any of the route fact
    lists in gather_facts() - top-by-volume, best/worst growth, best/worst
    price - since they all share the same per-route shape."""
    row = {
        "route": m["route"],
        "passenger_growth_pct": m["passenger_growth_pct"],
        "price_change_pct": m["price_change_pct"],
        "price_per_mile_change_pct": m["price_per_mile_change_pct"],
    }
    return build_route_sentence(row, m["leading_carrier"], m["leading_carrier_share_pct"])


def _route_efficiency_sentence(m: dict) -> str:
    """Plain-English line for one route's price-efficiency fact - a
    different shape/voice than _route_sentence_from_fact() since this is
    about pricing relative to distance peers, not growth or QoQ change."""
    direction = "above" if m["price_multiplier"] >= 1 else "below"
    pct_diff = abs(m["price_multiplier"] - 1) * 100
    return (
        f"{m['route']} ({m['distance_band']}, ~{m['distance']:,.0f} miles) prices "
        f"{pct_diff:.0f}% {direction} its distance-band peer average price per mile "
        f"(${m['price_per_mile']:.4f})."
    )


def build_carrier_sentence(row: dict) -> str:
    """Plain-English line for one carrier's rollup stats (total passengers,
    average price, price per mile, average market share) - used in the
    carrier best/worst sections, which have a different shape than the
    route-based lists."""
    return (
        f"{row['carrier']}: {row['total_passengers']:,.0f} total passengers, "
        f"average price ${row['avg_price']:.2f} (${row['avg_price_per_mile']:.4f}/mile), "
        f"average market share {row['avg_route_share_pct']:.1f}%."
    )


def build_carrier_growth_sentence(row: dict) -> str:
    """Plain-English line for one carrier's price-per-mile growth fact -
    quarter-over-quarter % change in that carrier's passenger-weighted price
    per mile (every route for that carrier combined), a different shape
    than build_carrier_sentence()'s snapshot stats."""
    return (
        f"{row['carrier']}: price per mile grew {row['price_per_mile_growth_pct']:+.1f}% "
        f"quarter over quarter to ${row['avg_price_per_mile']:.4f}, on "
        f"{row['total_passengers']:,.0f} total passengers."
    )


def _quarterly_trend_sentence(row: dict) -> str:
    """One sentence for a single quarter's whole-dataset rollup, reusing
    the same describe_growth()/describe_price_change() phrasing as the
    per-route sentences for a consistent voice."""
    growth_phrase = describe_growth(row.get("passenger_growth_pct"))
    price_phrase = describe_price_change(row.get("price_change_pct"))
    return (
        f"{row['year']} Q{row['quarter']}: {row['total_passengers']:,.0f} total passengers "
        f"across {row['total_routes']:,.0f} routes and {row['total_carriers']:,.0f} carriers, "
        f"{growth_phrase}, recording {price_phrase}. "
        f"Average price ${row['average_price']:.2f}, price per mile ${row['price_per_mile']:.4f}."
    )


def _short_route_blurb(routes: list) -> str:
    """One short blurb for a route-shaped fact list - just the single
    standout route, plus a brief mention if there are more, instead of one
    full sentence per row (which is fine in a table but too long for a
    blurb meant to sit underneath one)."""
    if not routes:
        return "No routes met the reporting threshold this quarter."
    lead = _route_sentence_from_fact(routes[0])
    if len(routes) > 1:
        return f"{lead} {len(routes) - 1} other route(s) also stood out this quarter."
    return lead


def _short_route_efficiency_blurb(routes: list) -> str:
    """Same as _short_route_blurb(), for the price-efficiency fact lists."""
    if not routes:
        return "No routes met the reporting threshold this quarter."
    lead = _route_efficiency_sentence(routes[0])
    if len(routes) > 1:
        return f"{lead} {len(routes) - 1} other route(s) also stood out this quarter."
    return lead


def _short_carrier_blurb(carriers: list) -> str:
    """Same as _short_route_blurb(), for the carrier-shaped fact lists."""
    if not carriers:
        return "No carriers met the reporting threshold this quarter."
    lead = build_carrier_sentence(carriers[0])
    if len(carriers) > 1:
        return f"{lead} {len(carriers) - 1} other carrier(s) also stood out this quarter."
    return lead


def _short_carrier_growth_blurb(carriers: list) -> str:
    """Same as _short_carrier_blurb(), for the carrier price-per-mile growth
    fact list."""
    if not carriers:
        return "No carriers met the reporting threshold this quarter."
    lead = build_carrier_growth_sentence(carriers[0])
    if len(carriers) > 1:
        return f"{lead} {len(carriers) - 1} other carrier(s) also stood out this quarter."
    return lead


def _fmt_pct(v, decimals: int = 0) -> str:
    """Shared formatter for the aggregate consensus stats - None (no
    qualifying data for that metric) renders as "n/a" instead of crashing
    or printing "None%"."""
    return "n/a" if v is None else f"{v:.{decimals}f}%"


def _build_consensus_template(facts: dict) -> dict:
    """Deterministic version of the restructured Overall Consensus: a short
    opening overview, an Insight/Takeaway pair each for Carrier
    Performance, Price Trends, and Route Performance, and a single
    Quarterly Trends paragraph - built only from the whole-population
    aggregate facts in facts["aggregate"] (see _compute_aggregate_facts())
    - no specific route or carrier names, matching the Claude engine's
    version and shape in _build_consensus_claude()."""
    agg = facts["aggregate"]
    trend = facts["quarterly_trend"]
    first_year, first_quarter = facts["first_year"], facts["first_quarter"]
    last_year, last_quarter = facts["last_year"], facts["last_quarter"]

    if trend:
        last_row = trend[-1]
        growth_phrase = describe_growth(last_row.get("passenger_growth_pct"))
        price_phrase = describe_price_change(last_row.get("price_change_pct"))
        opening = (
            f"{last_row['year']} Q{last_row['quarter']} showed {growth_phrase} across the "
            f"market, recording {price_phrase}, tracked from {first_year} Q{first_quarter} "
            f"through {last_year} Q{last_quarter}."
        )
    else:
        opening = "No quarterly data available to form a consensus this quarter."

    carrier_insight = (
        f"Across {agg['num_qualifying_carriers']} qualifying carriers, passenger volume "
        f"remains concentrated, with the leading carrier holding roughly "
        f"{_fmt_pct(agg['top_carrier_share_pct'])} of tracked passengers and the top three "
        f"combined holding about {_fmt_pct(agg['top3_carrier_share_pct'])}."
    )
    carrier_takeaway = (
        f"Only about {_fmt_pct(agg['pct_carriers_price_per_mile_growing'])} of carriers grew "
        f"price per mile this quarter even though roughly "
        f"{_fmt_pct(agg['pct_carriers_priced_above_avg_price_per_mile'])} are priced above the "
        "cross-carrier average - pricing power is being used selectively, worth watching "
        "whether that premium positioning holds as competition plays out."
    )

    mean_eff_str = "n/a" if agg["mean_price_multiplier"] is None else f"{agg['mean_price_multiplier']:.2f}x"
    price_insight = (
        f"Prices moved higher on about {_fmt_pct(agg['pct_routes_price_up'])} of qualifying "
        f"routes and lower on about {_fmt_pct(agg['pct_routes_price_down'])}, with a median "
        f"change of {_fmt_pct(agg['median_price_change_pct'], 1)} this quarter."
    )
    price_takeaway = (
        f"On a distance-adjusted basis only about {_fmt_pct(agg['pct_routes_priced_above_peers'])} "
        f"of routes are priced above their distance-band peer average (mean index {mean_eff_str}) - "
        "with prices softening on the majority of routes, revenue growth is likely being driven "
        "more by volume than by pricing power this quarter."
    )

    route_insight = (
        f"Of {agg['num_qualifying_routes']} qualifying routes, about "
        f"{_fmt_pct(agg['pct_routes_growing_passengers'])} grew passenger volume quarter over "
        f"quarter and about {_fmt_pct(agg['pct_routes_declining_passengers'])} declined, with "
        f"median passenger growth of {_fmt_pct(agg['median_passenger_growth_pct'], 1)}."
    )
    route_takeaway = (
        "Broad-based passenger growth is a positive signal, but it's worth confirming that "
        "volume is being captured at healthy prices rather than given away through discounting."
    )

    quarterly_paragraph = (
        _quarterly_trend_sentence(trend[-1]) if trend else "No quarterly trend data available this quarter."
    )

    return {
        "opening": opening,
        "Carrier Performance": {"insight": carrier_insight, "takeaway": carrier_takeaway},
        "Price Trends": {"insight": price_insight, "takeaway": price_takeaway},
        "Route Performance": {"insight": route_insight, "takeaway": route_takeaway},
        "Quarterly Trends": quarterly_paragraph,
    }


def _template_sections_from_facts(facts: dict) -> dict:
    """Build the template engine's per-section blurbs from already-gathered
    facts (see gather_facts()). Split out from build_summary_sections() so
    build_ai_summary_sections() can reuse it as a per-section fallback
    without a second round of database queries."""
    trend = facts["quarterly_trend"]
    quarterly_trend_blurb = (
        _quarterly_trend_sentence(trend[-1]) if trend else "No quarterly data to report."
    )

    return {
        "Quarterly Trends": quarterly_trend_blurb,
        "Top Routes by Volume": _short_route_blurb(facts["routes"]),
        "Best Passenger Growth": _short_route_blurb(facts["best_growth_routes"]),
        "Worst Passenger Growth": _short_route_blurb(facts["worst_growth_routes"]),
        "Best Price Movement": _short_route_blurb(facts["best_price_routes"]),
        "Worst Price Movement": _short_route_blurb(facts["worst_price_routes"]),
        "Best Price Efficiency": _short_route_efficiency_blurb(facts["best_price_efficiency_routes"]),
        "Worst Price Efficiency": _short_route_efficiency_blurb(facts["worst_price_efficiency_routes"]),
        "Top Carriers by Passengers": _short_carrier_blurb(facts["best_carrier_passengers"]),
        "Bottom Carriers by Passengers": _short_carrier_blurb(facts["worst_carrier_passengers"]),
        "Top Carriers by Price Efficiency": _short_carrier_blurb(facts["best_carrier_efficiency"]),
        "Top Carriers by Price-Per-Mile Growth": _short_carrier_growth_blurb(facts["carrier_price_per_mile_growth"]),
        "Top Carriers by Market Share": _short_carrier_blurb(facts["best_carrier_share"]),
        "Overall Consensus": _build_consensus_template(facts),
    }


def build_summary_sections(conn, top_n: int = 5) -> dict:
    """Template engine: deterministic Python, no API call, always available.

    Returns a dict of {section_title: blurb} - one short blurb per table in
    the PDF report (see SECTION_ORDER), so each can be placed directly under
    its matching table instead of one big block of text up front."""
    facts = gather_facts(conn, top_n)
    return _template_sections_from_facts(facts)


def _route_fact_line(m: dict) -> str:
    """One data line for a single route - reused across every route-based
    section sent to Claude (top-by-volume, best/worst growth, best/worst
    price), since they all share the same per-route fields."""
    growth = "n/a" if m["passenger_growth_pct"] is None or pd.isna(m["passenger_growth_pct"]) \
        else f"{m['passenger_growth_pct']:+.1f}%"
    price_chg = "n/a" if m["price_change_pct"] is None or pd.isna(m["price_change_pct"]) \
        else f"{m['price_change_pct']:+.1f}%"
    price_per_mile_chg = "n/a" if m["price_per_mile_change_pct"] is None or pd.isna(m["price_per_mile_change_pct"]) \
        else f"{m['price_per_mile_change_pct']:+.1f}%"
    carrier = m["leading_carrier"] or "n/a"
    share = "n/a" if m["leading_carrier_share_pct"] is None else f"{m['leading_carrier_share_pct']:.1f}%"

    return (
        f"- {m['route']}: {m['passengers']:,.0f} passengers, "
        f"passenger growth QoQ {growth}, average price ${m['average_price']:.2f}, "
        f"price change QoQ {price_chg}, price per mile ${m['price_per_mile']:.4f}, "
        f"price-per-mile change QoQ {price_per_mile_chg}, "
        f"leading carrier {carrier} ({share} share)."
    )


def _route_efficiency_fact_line(m: dict) -> str:
    """One data line for a single route's price-efficiency fact - reused
    across the Best/Worst Price Efficiency sections sent to Claude."""
    return (
        f"- {m['route']} ({m['distance_band']}, ~{m['distance']:,.0f} miles): "
        f"{m['passengers']:,.0f} passengers, price per mile ${m['price_per_mile']:.4f}, "
        f"{m['price_multiplier']:.2f}x its distance-band peer average."
    )


def _carrier_fact_line(c: dict) -> str:
    """One data line for a single carrier rollup - reused across every
    carrier-based section sent to Claude."""
    return (
        f"- {c['carrier']}: {c['total_passengers']:,.0f} total passengers, "
        f"average price ${c['avg_price']:.2f} (${c['avg_price_per_mile']:.4f}/mile), "
        f"average market share {c['avg_route_share_pct']:.1f}%."
    )


def _carrier_growth_fact_line(c: dict) -> str:
    """One data line for a single carrier's price-per-mile growth fact -
    reused in the Claude prompt for the Top Carriers by Price-Per-Mile
    Growth section, a different shape than _carrier_fact_line()."""
    return (
        f"- {c['carrier']}: {c['total_passengers']:,.0f} total passengers, "
        f"average price per mile ${c['avg_price_per_mile']:.4f}, "
        f"price-per-mile growth QoQ {c['price_per_mile_growth_pct']:+.1f}%."
    )


def _quarterly_trend_fact_line(row: dict) -> str:
    """One data line for a single quarter's whole-dataset rollup - same
    style as _route_fact_line/_carrier_fact_line, used for the quarterly
    trend section sent to Claude."""
    growth = "n/a" if row.get("passenger_growth_pct") is None or pd.isna(row.get("passenger_growth_pct")) \
        else f"{row['passenger_growth_pct']:+.1f}%"
    price_chg = "n/a" if row.get("price_change_pct") is None or pd.isna(row.get("price_change_pct")) \
        else f"{row['price_change_pct']:+.1f}%"
    return (
        f"- {row['year']} Q{row['quarter']}: {row['total_passengers']:,.0f} total passengers, "
        f"{row['total_routes']:,.0f} routes, {row['total_carriers']:,.0f} carriers, "
        f"passenger growth QoQ {growth}, "
        f"average price ${row['average_price']:.2f}, price change QoQ {price_chg}, "
        f"price per mile ${row['price_per_mile']:.4f}."
    )


def _section_facts_and_instruction(title: str, facts: dict) -> tuple:
    """Return (facts_text, instruction) for one section's Claude call - only
    the facts relevant to that section (tighter context, no topic-blending),
    plus what that blurb should focus on. Never called with "Overall
    Consensus" - that section is built separately by _build_consensus_claude()
    / _build_consensus_template(), which need the whole-population aggregate
    facts (facts["aggregate"]) rather than any one table's slice."""
    route_sections = {
        "Top Routes by Volume": ("routes", "the standout route(s) by passenger volume"),
        "Best Passenger Growth": ("best_growth_routes", "the fastest-growing route(s)"),
        "Worst Passenger Growth": ("worst_growth_routes", "the steepest-declining route(s)"),
        "Best Price Movement": (
            "best_price_routes",
            "the largest price increases (a price increase is positive - more revenue per passenger)",
        ),
        "Worst Price Movement": (
            "worst_price_routes",
            "the largest price decreases (a price decrease is negative - less revenue per passenger)",
        ),
    }
    route_efficiency_sections = {
        "Best Price Efficiency": (
            "best_price_efficiency_routes",
            "the route(s) priced highest relative to their distance-band peers",
        ),
        "Worst Price Efficiency": (
            "worst_price_efficiency_routes",
            "the route(s) priced lowest relative to their distance-band peers",
        ),
    }
    carrier_sections = {
        "Top Carriers by Passengers": ("best_carrier_passengers", "the carrier(s) with the highest passenger volume"),
        "Bottom Carriers by Passengers": ("worst_carrier_passengers", "the carrier(s) with the lowest passenger volume"),
        "Top Carriers by Price Efficiency": ("best_carrier_efficiency", "the carrier(s) with the highest average price per mile"),
        "Top Carriers by Market Share": ("best_carrier_share", "the carrier(s) with the highest average market share"),
    }

    if title == "Quarterly Trends":
        lines = [_quarterly_trend_fact_line(r) for r in facts["quarterly_trend"]]
        instruction = "Compare the most recent quarter to the previous quarter, not a quarter-by-quarter recap."
    elif title in route_sections:
        facts_key, what = route_sections[title]
        lines = [_route_fact_line(m) for m in facts[facts_key]]
        instruction = f"Highlight {what}, calling them out by name."
    elif title in route_efficiency_sections:
        facts_key, what = route_efficiency_sections[title]
        lines = [_route_efficiency_fact_line(m) for m in facts[facts_key]]
        instruction = f"Highlight {what}, calling them out by name."
    elif title == "Top Carriers by Price-Per-Mile Growth":
        lines = [_carrier_growth_fact_line(c) for c in facts["carrier_price_per_mile_growth"]]
        instruction = "Highlight the carrier(s) with the fastest price-per-mile growth, calling them out by name."
    elif title in carrier_sections:
        facts_key, what = carrier_sections[title]
        lines = [_carrier_fact_line(c) for c in facts[facts_key]]
        instruction = f"Highlight {what}, calling them out by name."
    else:
        raise ValueError(f"Unknown section title: {title}")

    facts_text = "\n".join(lines) if lines else "(no data available for this section this quarter)"
    return facts_text, instruction


def _aggregate_facts_text(agg: dict) -> str:
    """Render _compute_aggregate_facts() output as compact text for the
    Overall Consensus Claude prompt - whole-population aggregate numbers
    only, no specific route or carrier names (those are already covered in
    their own sections' blurbs)."""

    def fmt(v, decimals: int = 1):
        return "n/a" if v is None else f"{v:.{decimals}f}%"

    mean_eff = agg["mean_price_multiplier"]
    mean_eff_str = "n/a" if mean_eff is None else f"{mean_eff:.2f}"

    return "\n".join(
        [
            f"Qualifying routes: {agg['num_qualifying_routes']}",
            f"Routes with passenger growth QoQ: {fmt(agg['pct_routes_growing_passengers'], 0)}",
            f"Routes with passenger decline QoQ: {fmt(agg['pct_routes_declining_passengers'], 0)}",
            f"Median passenger growth QoQ: {fmt(agg['median_passenger_growth_pct'])}",
            f"Routes with price increase QoQ: {fmt(agg['pct_routes_price_up'], 0)}",
            f"Routes with price decrease QoQ: {fmt(agg['pct_routes_price_down'], 0)}",
            f"Median price change QoQ: {fmt(agg['median_price_change_pct'])}",
            f"Routes priced above distance-band peer average: {fmt(agg['pct_routes_priced_above_peers'], 0)}",
            f"Mean price-efficiency index (1.0 = at peer average): {mean_eff_str}",
            f"Qualifying carriers: {agg['num_qualifying_carriers']}",
            f"Leading carrier's share of qualifying passenger volume: {fmt(agg['top_carrier_share_pct'], 0)}",
            f"Top 3 carriers' combined share of qualifying passenger volume: {fmt(agg['top3_carrier_share_pct'], 0)}",
            f"Carriers with price-per-mile growth QoQ: {fmt(agg['pct_carriers_price_per_mile_growing'], 0)}",
            "Carriers priced above the cross-carrier average price per mile: "
            f"{fmt(agg['pct_carriers_priced_above_avg_price_per_mile'], 0)}",
        ]
    )


def _build_consensus_claude(client, model: str, facts: dict) -> dict:
    """Claude-engine version of the restructured Overall Consensus: one call
    for the short opening overview, one call for the Quarterly Trends
    paragraph, and two calls each (Insight then Takeaway) for Carrier
    Performance, Price Trends, and Route Performance - 8 calls total. Every
    call is given only whole-population aggregate facts (see
    _compute_aggregate_facts()) - never the named best/worst routes or
    carriers already covered in their own sections, so this reads as a
    distinct market-wide summary rather than a repeat of those call-outs.
    Returns {"opening": str, "Quarterly Trends": str, "Carrier Performance":
    {"insight": str, "takeaway": str}, "Price Trends": {...}, "Route
    Performance": {...}} - see _build_consensus_template() for the
    deterministic engine's version of the same shape."""
    agg_text = _aggregate_facts_text(facts["aggregate"])
    quarterly_text = "\n".join(_quarterly_trend_fact_line(r) for r in facts["quarterly_trend"])

    # max_tokens=500 on every call, not just the subsections - the opening
    # was previously capped at 120, which was too tight even for its
    # instructed 1-2 sentences and truncated mid-word. 500 leaves generous
    # headroom above the instructed sentence counts below so nothing gets
    # cut off mid-sentence again, even if a response runs a bit long.
    def call(instruction: str, facts_text: str, max_tokens: int = 500) -> str:
        prompt = (
            "You are writing part of the \"Overall Consensus\" section of an airline market "
            "analytics report, placed on its own page at the end of the report for a busy "
            "executive audience. Using ONLY the facts below - do not invent any numbers - "
            "write in plain prose (no bullet points, no headers, no bold text, no preamble). "
            "Do not name any specific route or carrier - describe overall patterns only, "
            "since individual standouts are already covered elsewhere in the report. Write "
            "in plain, direct language an executive can skim in seconds - no jargon, no "
            "hedging, no filler. This is written from the company's revenue perspective: "
            "price increases and pricing above peer average are positive.\n\n"
            f"{instruction}\n\n{facts_text}"
        )
        response = client.messages.create(
            model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def insight_and_takeaway(topic: str, facts_text: str) -> dict:
        """Two calls for one subsection instead of one paragraph: a plain
        Insight (the pattern + supporting numbers, no recommendation), then
        a Takeaway written with that insight as context so it builds on it
        directly - what it implies or what to watch/do - instead of
        repeating the same numbers a second time. Used for Carrier
        Performance, Price Trends, and Route Performance only; Quarterly
        Trends and the opening stay single paragraphs."""
        insight = call(
            f"In 2 short sentences, state the single key insight on {topic}. Cite one "
            "or two supporting numbers. Describe the pattern only - no recommendation "
            "or action yet, that comes next.",
            facts_text,
        )
        takeaway = call(
            f"The insight already written for this section is: \"{insight}\"\n\n"
            "In 1-2 short sentences, give the implication or action/watch-item that "
            "follows from that insight. Don't repeat the numbers already stated above - "
            "focus on what it means or what to do about it.",
            facts_text,
        )
        return {"insight": insight, "takeaway": takeaway}

    opening = call(
        "Write a 1-2 sentence general overview of the market this quarter.",
        quarterly_text,
    )
    carrier = insight_and_takeaway(
        "the overall carrier landscape (concentration, price-efficiency direction, "
        "price-per-mile growth breadth, market share concentration)",
        agg_text,
    )
    price = insight_and_takeaway(
        "overall price movement and price-efficiency patterns across routes",
        agg_text,
    )
    route = insight_and_takeaway(
        "overall passenger growth patterns across routes",
        agg_text,
    )
    quarterly_paragraph = call(
        "In 3-4 short sentences, give the single key insight on the overall trajectory "
        "this quarter versus the prior quarter (passengers, prices, carrier count), and "
        "what it implies going forward. Don't try to cover every fact listed below.",
        quarterly_text,
    )

    return {
        "opening": opening,
        "Carrier Performance": carrier,
        "Price Trends": price,
        "Route Performance": route,
        "Quarterly Trends": quarterly_paragraph,
    }


def _call_claude_for_section(client, model: str, title: str, facts: dict) -> str:
    """One Claude API call for a single section's blurb - returns plain text
    directly as the blurb, no JSON involved, so there's nothing that can
    fail to parse. See _section_facts_and_instruction() for what data and
    instruction each section gets."""
    facts_text, instruction = _section_facts_and_instruction(title, facts)

    prompt = (
        "You are writing one short blurb for an airline market analytics report for "
        "company leadership. This blurb will be placed directly underneath its "
        "matching data table in the final report, so comment on and interpret what "
        "the data shows - don't just restate every row - in one to three sentences "
        "of plain prose (no bullet points, no headers, no bold text, no preamble, "
        "just the blurb itself).\n\n"
        "Using ONLY the facts below - do not invent any numbers, routes, or carriers "
        f"not listed - write the blurb for the \"{title}\" section. {instruction}\n\n"
        "This is written from the company's revenue perspective, not the traveler's: "
        "a price increase is a positive (more revenue per passenger), and a price "
        "decrease is a negative (less revenue per passenger). Similarly, a route or "
        "carrier priced above its price-efficiency peer average is a positive.\n\n"
        f"{facts_text}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def build_ai_summary_sections(conn, top_n: int = 5, model: str = "claude-sonnet-5") -> dict:
    """Claude engine: makes one Claude API call per section in SECTION_ORDER
    except "Overall Consensus" (each returning plain text directly as that
    section's blurb - no JSON, so there's nothing to parse or break), then
    a separate set of 8 calls for "Overall Consensus" via
    _build_consensus_claude() (opening + Quarterly Trends + Insight/
    Takeaway pairs for Carrier Performance, Price Trends, and Route
    Performance, returned as a dict instead of a string). Each call only
    sees the facts relevant to its own section/subsection. Each call fails
    independently: if one fails for any reason (network, rate limit, etc.),
    only that piece falls back to the template engine's version - printing
    a warning first - rather than the whole report falling back. Requires
    ANTHROPIC_API_KEY and `pip install anthropic`."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "Missing dependency: anthropic. Install it with: pip install anthropic --break-system-packages"
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Get a key from https://console.anthropic.com "
            "and put it in .env (or export it), then try again."
        )

    facts = gather_facts(conn, top_n)
    template_fallback = _template_sections_from_facts(facts)
    client = anthropic.Anthropic(api_key=api_key)

    sections = {}
    for title in SECTION_ORDER:
        if title == "Overall Consensus":
            continue
        try:
            sections[title] = _call_claude_for_section(client, model, title, facts)
        except Exception as e:
            print(f"WARNING: Claude call for '{title}' failed ({e}). Using template blurb instead.", file=sys.stderr)
            sections[title] = template_fallback[title]

    try:
        sections["Overall Consensus"] = _build_consensus_claude(client, model, facts)
    except Exception as e:
        print(f"WARNING: Claude calls for 'Overall Consensus' failed ({e}). Using template version instead.", file=sys.stderr)
        sections["Overall Consensus"] = template_fallback["Overall Consensus"]

    return sections


def generate_narrative(conn, top_n: int = 5, engine: str = None, model: str = "claude-sonnet-5") -> dict:
    """Single entry point used by both this script's CLI and
    reports/generate_reports.py. Returns a dict of {section_title: blurb},
    one entry per table in the report plus "Overall Consensus" - see SECTION_ORDER for
    the exact keys. Engine resolution order: explicit `engine` argument,
    then the SUMMARY_ENGINE environment variable, then "template".

    If engine="claude" but a package/API key problem stops it before any
    section calls can run, this falls back to the template engine entirely.
    Individual section failures (network error, rate limit, etc. on one of
    the per-section calls) are handled inside build_ai_summary_sections()
    itself, only that section falls back, not the whole report. Either way,
    a warning prints first, so a fallback is never invisible."""
    engine = engine or os.environ.get("SUMMARY_ENGINE", "template")
    if engine == "claude":
        try:
            return build_ai_summary_sections(conn, top_n=top_n, model=model)
        except Exception as e:
            print(f"WARNING: Claude summary failed ({e}). Falling back to template engine.", file=sys.stderr)
    return build_summary_sections(conn, top_n=top_n)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an executive summary from analytics tables.")
    parser.add_argument("--top", type=int, default=5, help="Number of top routes to summarize (default: 5)")
    parser.add_argument(
        "--engine",
        choices=["template", "claude"],
        default=None,
        help="Narrative engine: 'template' (deterministic, no API key) or 'claude' (Claude API). "
        "Defaults to the SUMMARY_ENGINE env var, or 'template' if that's unset.",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-5",
        help="Claude model to use when --engine claude (default: claude-sonnet-5)",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        sections = generate_narrative(conn, top_n=args.top, engine=args.engine, model=args.model)
    finally:
        conn.close()

    for title in SECTION_ORDER:
        print(title)
        print("-" * len(title))
        blurb = sections.get(title, "")
        if title == "Overall Consensus" and isinstance(blurb, dict):
            print(blurb.get("opening", ""))
            for label in ("Carrier Performance", "Price Trends", "Route Performance"):
                sub = blurb.get(label, {})
                print(f"\n{label}\nInsight: {sub.get('insight', '')}\nTakeaway: {sub.get('takeaway', '')}")
            print(f"\nQuarterly Trends\n{blurb.get('Quarterly Trends', '')}")
        else:
            print(blurb)
        print()


if __name__ == "__main__":
    main()
