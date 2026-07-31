"""
summarize.py

Generates a narrative executive summary from the market_summary,
carrier_summary, and fare_variance_summary tables.

Two engines produce the narrative from those same facts:

  template  - deterministic Python (describe_growth/describe_fare_change
              template phrases into sentences). No API key needed, always
              works, wording is repetitive across runs.
  claude    - makes one Claude API call per section (13 total, see
              SECTION_ORDER), each returning that section's blurb as plain
              text. Requires ANTHROPIC_API_KEY and the `anthropic` package.
              More natural/varied writing, but is 13 external API calls;
              each section fails independently and falls back to the
              template engine's version of just that section if it does.

Both engines return a dict of {section_title: blurb} - one short blurb per
table in the PDF report (see SECTION_ORDER), plus "Overall Consensus" for the overall
consensus paragraph - so reports/generate_reports.py can place each blurb
directly under its matching table instead of one big block of text. Both
engines cover the same ground: the whole-dataset quarterly trend first,
then top markets by volume, best/worst passenger growth, best/worst fare
movement (company-revenue framing: a fare increase is "best"), and
best/worst carriers by passenger volume, average fare, and market share.

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
    python summarize.py --top 10                  # top 10 markets instead of 5
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

# Growth/fare-change % thresholds used to pick descriptive language.
STRONG_GROWTH_PCT = 10.0
DECLINE_PCT = -5.0
STABLE_FARE_BAND_PCT = 3.0
FARE_INCREASE_PCT = 10.0

# Minimum total passengers a carrier needs (that quarter) to be eligible for
# the average-fare / average-market-share best/worst rankings. Without this,
# a carrier with a single low-volume appearance can average out to a
# misleading 100% market share and show up as a "top" performer. Doesn't
# affect the full Carrier Summary listing or the by-passengers rankings,
# where showing genuinely small carriers is the point.
MIN_CARRIER_PASSENGERS = 100

# Minimum current-quarter passengers a market needs to be eligible for the
# best/worst passenger-growth and fare-movement rankings. Separate from (and
# higher than) the 100-passenger prior-quarter floor transform.py already
# uses to decide whether growth/change % gets computed at all - that check
# only looks at the prior quarter, so a market can go from ~110 to 900
# passengers and still post an eye-catching (but not very meaningful)
# triple-digit percentage swing. Doesn't affect "Top Markets by Volume",
# which is inherently high-volume already.
MIN_MARKET_PASSENGERS = 5000


def get_quarter_range(conn) -> tuple:
    """Return (first_year, first_quarter, last_year, last_quarter) present
    in market_summary, used to phrase the "From ... through ..." lead-in."""
    sql = """
        SELECT MIN(year * 10 + quarter) AS first_key,
               MAX(year * 10 + quarter) AS last_key
        FROM market_summary;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        first_key, last_key = cur.fetchone()
    return (first_key // 10, first_key % 10, last_key // 10, last_key % 10)


def get_quarterly_trend(conn) -> pd.DataFrame:
    """Whole-dataset rollup: one row per (year, quarter) across every
    quarter in market_summary - not per-market, every market combined.
    Same KPI set as the original Tableau Executive Overview page: total
    passengers, total markets, passenger-weighted average fare, and
    passenger-weighted fare per mile. passenger_growth_pct/fare_change_pct
    are quarter-over-quarter change computed in pandas afterward (first
    quarter has no prior quarter, so its value is NaN)."""
    sql = """
        SELECT
            year,
            quarter,
            SUM(passengers) AS total_passengers,
            COUNT(DISTINCT market) AS total_markets,
            SUM(average_fare * passengers) / SUM(passengers) AS average_fare,
            SUM(fare_per_mile * passengers) / SUM(passengers) AS fare_per_mile
        FROM market_summary
        GROUP BY year, quarter
        ORDER BY year, quarter;
    """
    df = pd.read_sql_query(sql, conn)
    df["passenger_growth_pct"] = df["total_passengers"].pct_change() * 100
    df["fare_change_pct"] = df["average_fare"].pct_change() * 100
    return df


def get_top_markets(conn, latest_year: int, latest_quarter: int, top_n: int = None) -> pd.DataFrame:
    """Top N markets in the latest quarter by passenger volume, joined with
    that market's fare_variance_summary row for fare_change_pct. Pass
    top_n=None for every market with no limit (used for the unlimited xlsx
    sheets; the narrative engines always pass an explicit top_n)."""
    limit_clause = "LIMIT %s" if top_n is not None else ""
    params = (latest_year, latest_quarter) if top_n is None else (latest_year, latest_quarter, top_n)
    sql = f"""
        SELECT
            m.market,
            m.passengers,
            m.passenger_growth_pct,
            m.average_fare,
            m.fare_per_mile,
            f.fare_change_pct,
            f.fare_per_mile_change_pct
        FROM market_summary m
        LEFT JOIN fare_variance_summary f
            ON f.market = m.market AND f.year = m.year AND f.quarter = m.quarter
        WHERE m.year = %s AND m.quarter = %s
        ORDER BY m.passengers DESC
        {limit_clause};
    """
    return pd.read_sql_query(sql, conn, params=params)


def get_leading_carrier(conn, market: str, year: int, quarter: int):
    """Return (carrier, market_share_pct) for the top carrier in a given
    market + quarter, or (None, None) if no carrier rows exist."""
    sql = """
        SELECT carrier, market_share_pct
        FROM carrier_summary
        WHERE market = %s AND year = %s AND quarter = %s
        ORDER BY market_share_pct DESC
        LIMIT 1;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (market, year, quarter))
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


# Columns get_ranked_markets()/get_ranked_carriers() are allowed to sort by.
# Whitelisted (not user input) since the column name is interpolated
# directly into the SQL string - only ever called with these fixed values.
_RANKABLE_MARKET_COLUMNS = {"passenger_growth_pct": "m", "fare_change_pct": "f"}
_RANKABLE_CARRIER_COLUMNS = {"total_passengers", "avg_fare", "avg_market_share_pct"}


def get_ranked_markets(
    conn, year: int, quarter: int, order_by: str, ascending: bool, top_n: int = None,
    min_passengers: int = None,
) -> pd.DataFrame:
    """Same market_summary + fare_variance_summary join/shape as
    get_top_markets(), but ranked by passenger_growth_pct or fare_change_pct
    instead of passenger volume, and excluding markets with no value for
    that column (thin routes with no prior-quarter comparison). Pass
    top_n=None for every qualifying market with no limit. Pass
    min_passengers to also require the market's current-quarter passenger
    count meet a floor - separate from (and independent of) the
    prior-quarter floor transform.py already applies before computing
    growth/change % at all, so a market that jumped from a tiny prior
    quarter to a still-small current quarter can't post an eye-catching but
    not very meaningful percentage swing."""
    if order_by not in _RANKABLE_MARKET_COLUMNS:
        raise ValueError(f"order_by must be one of {sorted(_RANKABLE_MARKET_COLUMNS)}")
    table_alias = _RANKABLE_MARKET_COLUMNS[order_by]
    direction = "ASC" if ascending else "DESC"
    passenger_filter = "AND m.passengers >= %s" if min_passengers is not None else ""
    limit_clause = "LIMIT %s" if top_n is not None else ""
    params = [year, quarter]
    if min_passengers is not None:
        params.append(min_passengers)
    if top_n is not None:
        params.append(top_n)
    sql = f"""
        SELECT
            m.market,
            m.passengers,
            m.passenger_growth_pct,
            m.average_fare,
            m.fare_per_mile,
            f.fare_change_pct,
            f.fare_per_mile_change_pct
        FROM market_summary m
        LEFT JOIN fare_variance_summary f
            ON f.market = m.market AND f.year = m.year AND f.quarter = m.quarter
        WHERE m.year = %s AND m.quarter = %s
          AND {table_alias}.{order_by} IS NOT NULL
          {passenger_filter}
        ORDER BY {table_alias}.{order_by} {direction}
        {limit_clause};
    """
    return pd.read_sql_query(sql, conn, params=tuple(params))


def get_ranked_carriers(
    conn, year: int, quarter: int, order_by: str, ascending: bool, top_n: int = None,
    min_passengers: int = None,
) -> pd.DataFrame:
    """Carrier-level rollup (same shape as generate_reports.py's former
    fetch_carrier_performance): total passengers, passenger-weighted average
    fare, and average market share per carrier, ranked by whichever of
    those three columns is requested. Pass top_n=None for every carrier
    with no limit. Pass min_passengers to exclude carriers whose total
    passengers that quarter fall below the threshold - used for the
    avg_fare/avg_market_share_pct rankings so a carrier with a single
    low-volume appearance can't average out to a misleading 100% share and
    rank as a "top" performer."""
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
               SUM(average_fare * passengers) / SUM(passengers) AS avg_fare,
               AVG(market_share_pct) AS avg_market_share_pct
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


def describe_fare_change(pct) -> str:
    if pct is None or pd.isna(pct):
        return "no prior-quarter fare data"
    if abs(pct) <= STABLE_FARE_BAND_PCT:
        return "stable fare levels"
    if pct >= FARE_INCREASE_PCT:
        return f"a notable fare increase (+{pct:.1f}%)"
    if pct > 0:
        return f"a modest fare increase (+{pct:.1f}%)"
    return f"lower fares ({pct:.1f}%)"


def describe_fare_per_mile_change(pct) -> str:
    """Same thresholds as describe_fare_change, worded around per-mile
    pricing efficiency rather than the flat average fare."""
    if pct is None or pd.isna(pct):
        return None
    if abs(pct) <= STABLE_FARE_BAND_PCT:
        return "fare-per-mile efficiency held steady"
    if pct >= FARE_INCREASE_PCT:
        return f"fare-per-mile rose sharply (+{pct:.1f}%)"
    if pct > 0:
        return f"fare-per-mile edged up (+{pct:.1f}%)"
    return f"fare-per-mile improved ({pct:.1f}%)"


def build_market_sentence(row, carrier, market_share) -> str:
    growth_phrase = describe_growth(row["passenger_growth_pct"])
    fare_phrase = describe_fare_change(row["fare_change_pct"])
    fare_per_mile_phrase = describe_fare_per_mile_change(row.get("fare_per_mile_change_pct"))

    sentence = f"{row['market']} showed {growth_phrase} while recording {fare_phrase}."
    if fare_per_mile_phrase:
        sentence += f" {fare_per_mile_phrase.capitalize()}."
    if carrier:
        sentence += f" {carrier} led the market with {market_share:.1f}% share."
    return sentence


def _build_market_facts(conn, df: pd.DataFrame, year: int, quarter: int) -> list:
    """Turn a market_summary/fare_variance_summary query result into the
    per-market fact dicts used throughout this module, adding each market's
    leading carrier. Shared by every "list of markets" section in
    gather_facts() so they all carry the same fields."""
    markets = []
    for _, row in df.iterrows():
        carrier, market_share = get_leading_carrier(conn, row["market"], year, quarter)
        markets.append(
            {
                "market": row["market"],
                "passengers": row["passengers"],
                "passenger_growth_pct": row["passenger_growth_pct"],
                "average_fare": row["average_fare"],
                "fare_per_mile": row["fare_per_mile"],
                "fare_change_pct": row["fare_change_pct"],
                "fare_per_mile_change_pct": row["fare_per_mile_change_pct"],
                "leading_carrier": carrier,
                "leading_carrier_share_pct": market_share,
            }
        )
    return markets


# Fixed top-N for the best/worst market sections below, per the agreed
# design (separate from `top_n`, which only controls the original
# top-by-volume "markets" list used by both engines today).
BEST_WORST_TOP_N = 3

# Same idea, but for the six carrier best/worst sections (by passengers,
# average fare, and market share) - kept separate from BEST_WORST_TOP_N so
# the carrier tables can show more rows without affecting the market
# growth/fare tables.
CARRIER_BEST_WORST_TOP_N = 5

# Both narrative engines return a dict of {section_title: blurb}, one entry
# per table in the PDF report plus "Overall Consensus" (the standalone wrap-up
# paragraph, not tied to any one table). Keys match the table titles used in
# reports/generate_reports.py's DOMAIN_GROUPS exactly, so a blurb can be
# looked up and placed directly under its matching table.
SECTION_ORDER = [
    "Quarterly Trends",
    "Top Markets by Volume",
    "Best Passenger Growth",
    "Worst Passenger Growth",
    "Best Fare Movement",
    "Worst Fare Movement",
    "Top Carriers by Passengers",
    "Bottom Carriers by Passengers",
    "Top Carriers by Average Fare",
    "Bottom Carriers by Average Fare",
    "Top Carriers by Market Share",
    "Bottom Carriers by Market Share",
    "Overall Consensus",
]


def gather_facts(conn, top_n: int = 5) -> dict:
    """Pull the raw numbers both the template engine and the Claude engine
    build their narrative from, so the two engines never disagree on facts
    - only on how the facts are worded.

    "quarterly_trend" is the whole dataset (every market combined) rolled
    up to one row per quarter, read first by both engines before anything
    market/carrier-specific."""
    first_year, first_quarter, last_year, last_quarter = get_quarter_range(conn)

    quarterly_trend = get_quarterly_trend(conn).to_dict("records")

    top_markets_df = get_top_markets(conn, last_year, last_quarter, top_n)
    markets = _build_market_facts(conn, top_markets_df, last_year, last_quarter)

    best_growth_df = get_ranked_markets(
        conn, last_year, last_quarter, "passenger_growth_pct", ascending=False, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_MARKET_PASSENGERS,
    )
    worst_growth_df = get_ranked_markets(
        conn, last_year, last_quarter, "passenger_growth_pct", ascending=True, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_MARKET_PASSENGERS,
    )
    # Company-revenue framing: fares UP is "best" (more revenue per
    # passenger), fares DOWN is "worst" (less revenue per passenger).
    best_fare_df = get_ranked_markets(
        conn, last_year, last_quarter, "fare_change_pct", ascending=False, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_MARKET_PASSENGERS,
    )
    worst_fare_df = get_ranked_markets(
        conn, last_year, last_quarter, "fare_change_pct", ascending=True, top_n=BEST_WORST_TOP_N,
        min_passengers=MIN_MARKET_PASSENGERS,
    )

    best_growth_markets = _build_market_facts(conn, best_growth_df, last_year, last_quarter)
    worst_growth_markets = _build_market_facts(conn, worst_growth_df, last_year, last_quarter)
    best_fare_markets = _build_market_facts(conn, best_fare_df, last_year, last_quarter)
    worst_fare_markets = _build_market_facts(conn, worst_fare_df, last_year, last_quarter)

    best_carrier_passengers = get_ranked_carriers(
        conn, last_year, last_quarter, "total_passengers", ascending=False, top_n=CARRIER_BEST_WORST_TOP_N
    ).to_dict("records")
    worst_carrier_passengers = get_ranked_carriers(
        conn, last_year, last_quarter, "total_passengers", ascending=True, top_n=CARRIER_BEST_WORST_TOP_N
    ).to_dict("records")
    best_carrier_fare = get_ranked_carriers(
        conn, last_year, last_quarter, "avg_fare", ascending=False, top_n=CARRIER_BEST_WORST_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")
    worst_carrier_fare = get_ranked_carriers(
        conn, last_year, last_quarter, "avg_fare", ascending=True, top_n=CARRIER_BEST_WORST_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")
    best_carrier_share = get_ranked_carriers(
        conn, last_year, last_quarter, "avg_market_share_pct", ascending=False, top_n=CARRIER_BEST_WORST_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")
    worst_carrier_share = get_ranked_carriers(
        conn, last_year, last_quarter, "avg_market_share_pct", ascending=True, top_n=CARRIER_BEST_WORST_TOP_N,
        min_passengers=MIN_CARRIER_PASSENGERS,
    ).to_dict("records")

    return {
        "first_year": first_year,
        "first_quarter": first_quarter,
        "last_year": last_year,
        "last_quarter": last_quarter,
        "quarterly_trend": quarterly_trend,
        "markets": markets,
        "best_growth_markets": best_growth_markets,
        "worst_growth_markets": worst_growth_markets,
        "best_fare_markets": best_fare_markets,
        "worst_fare_markets": worst_fare_markets,
        "best_carrier_passengers": best_carrier_passengers,
        "worst_carrier_passengers": worst_carrier_passengers,
        "best_carrier_fare": best_carrier_fare,
        "worst_carrier_fare": worst_carrier_fare,
        "best_carrier_share": best_carrier_share,
        "worst_carrier_share": worst_carrier_share,
    }


def _market_sentence_from_fact(m: dict) -> str:
    """Adapter so build_market_sentence() (which expects a plain dict with
    just the fields it reads) can be reused for any of the market fact
    lists in gather_facts() - top-by-volume, best/worst growth, best/worst
    fare - since they all share the same per-market shape."""
    row = {
        "market": m["market"],
        "passenger_growth_pct": m["passenger_growth_pct"],
        "fare_change_pct": m["fare_change_pct"],
        "fare_per_mile_change_pct": m["fare_per_mile_change_pct"],
    }
    return build_market_sentence(row, m["leading_carrier"], m["leading_carrier_share_pct"])


def build_carrier_sentence(row: dict) -> str:
    """Plain-English line for one carrier's rollup stats (total passengers,
    average fare, average market share) - used in the carrier best/worst
    sections, which have a different shape than the market-based lists."""
    return (
        f"{row['carrier']}: {row['total_passengers']:,.0f} total passengers, "
        f"average fare ${row['avg_fare']:.2f}, average market share {row['avg_market_share_pct']:.1f}%."
    )


def _quarterly_trend_sentence(row: dict) -> str:
    """One sentence for a single quarter's whole-dataset rollup, reusing
    the same describe_growth()/describe_fare_change() phrasing as the
    per-market sentences for a consistent voice."""
    growth_phrase = describe_growth(row.get("passenger_growth_pct"))
    fare_phrase = describe_fare_change(row.get("fare_change_pct"))
    return (
        f"{row['year']} Q{row['quarter']}: {row['total_passengers']:,.0f} total passengers "
        f"across {row['total_markets']:,.0f} markets, {growth_phrase}, recording {fare_phrase}. "
        f"Average fare ${row['average_fare']:.2f}, fare per mile ${row['fare_per_mile']:.4f}."
    )


def _short_market_blurb(markets: list) -> str:
    """One short blurb for a market-shaped fact list - just the single
    standout market, plus a brief mention if there are more, instead of one
    full sentence per row (which is fine in a table but too long for a
    blurb meant to sit underneath one)."""
    if not markets:
        return "No markets met the reporting threshold this quarter."
    lead = _market_sentence_from_fact(markets[0])
    if len(markets) > 1:
        return f"{lead} {len(markets) - 1} other market(s) also stood out this quarter."
    return lead


def _short_carrier_blurb(carriers: list) -> str:
    """Same as _short_market_blurb(), for the carrier-shaped fact lists."""
    if not carriers:
        return "No carriers met the reporting threshold this quarter."
    lead = build_carrier_sentence(carriers[0])
    if len(carriers) > 1:
        return f"{lead} {len(carriers) - 1} other carrier(s) also stood out this quarter."
    return lead


def _template_sections_from_facts(facts: dict) -> dict:
    """Build the template engine's per-section blurbs from already-gathered
    facts (see gather_facts()). Split out from build_summary_sections() so
    build_ai_summary_sections() can reuse it as a per-section fallback
    without a second round of database queries."""
    first_year, first_quarter = facts["first_year"], facts["first_quarter"]
    last_year, last_quarter = facts["last_year"], facts["last_quarter"]

    trend = facts["quarterly_trend"]
    quarterly_trend_blurb = (
        _quarterly_trend_sentence(trend[-1]) if trend else "No quarterly data to report."
    )

    # Data-aware consensus - references the latest quarter's actual growth/
    # fare direction and market count, instead of a fixed sentence that never
    # changes regardless of what the data actually shows.
    if trend:
        last_row = trend[-1]
        growth_phrase = describe_growth(last_row.get("passenger_growth_pct"))
        fare_phrase = describe_fare_change(last_row.get("fare_change_pct"))
        consensus = (
            f"{last_row['year']} Q{last_row['quarter']} showed {growth_phrase}, recording "
            f"{fare_phrase}, across {last_row['total_markets']:,.0f} markets tracked from "
            f"{first_year} Q{first_quarter} through {last_year} Q{last_quarter}. Leadership "
            "should watch the standout markets and carriers highlighted above for where "
            "attention may be needed."
        )
    else:
        consensus = "No quarterly data available to form a consensus this quarter."

    return {
        "Quarterly Trends": quarterly_trend_blurb,
        "Top Markets by Volume": _short_market_blurb(facts["markets"]),
        "Best Passenger Growth": _short_market_blurb(facts["best_growth_markets"]),
        "Worst Passenger Growth": _short_market_blurb(facts["worst_growth_markets"]),
        "Best Fare Movement": _short_market_blurb(facts["best_fare_markets"]),
        "Worst Fare Movement": _short_market_blurb(facts["worst_fare_markets"]),
        "Top Carriers by Passengers": _short_carrier_blurb(facts["best_carrier_passengers"]),
        "Bottom Carriers by Passengers": _short_carrier_blurb(facts["worst_carrier_passengers"]),
        "Top Carriers by Average Fare": _short_carrier_blurb(facts["best_carrier_fare"]),
        "Bottom Carriers by Average Fare": _short_carrier_blurb(facts["worst_carrier_fare"]),
        "Top Carriers by Market Share": _short_carrier_blurb(facts["best_carrier_share"]),
        "Bottom Carriers by Market Share": _short_carrier_blurb(facts["worst_carrier_share"]),
        "Overall Consensus": consensus,
    }


def build_summary_sections(conn, top_n: int = 5) -> dict:
    """Template engine: deterministic Python, no API call, always available.

    Returns a dict of {section_title: blurb} - one short blurb per table in
    the PDF report (see SECTION_ORDER), so each can be placed directly under
    its matching table instead of one big block of text up front."""
    facts = gather_facts(conn, top_n)
    return _template_sections_from_facts(facts)


def _market_fact_line(m: dict) -> str:
    """One data line for a single market - reused across every market-based
    section sent to Claude (top-by-volume, best/worst growth, best/worst
    fare), since they all share the same per-market fields."""
    growth = "n/a" if m["passenger_growth_pct"] is None or pd.isna(m["passenger_growth_pct"]) \
        else f"{m['passenger_growth_pct']:+.1f}%"
    fare_chg = "n/a" if m["fare_change_pct"] is None or pd.isna(m["fare_change_pct"]) \
        else f"{m['fare_change_pct']:+.1f}%"
    fare_per_mile_chg = "n/a" if m["fare_per_mile_change_pct"] is None or pd.isna(m["fare_per_mile_change_pct"]) \
        else f"{m['fare_per_mile_change_pct']:+.1f}%"
    carrier = m["leading_carrier"] or "n/a"
    share = "n/a" if m["leading_carrier_share_pct"] is None else f"{m['leading_carrier_share_pct']:.1f}%"

    return (
        f"- {m['market']}: {m['passengers']:,.0f} passengers, "
        f"passenger growth QoQ {growth}, average fare ${m['average_fare']:.2f}, "
        f"fare change QoQ {fare_chg}, fare per mile ${m['fare_per_mile']:.4f}, "
        f"fare-per-mile change QoQ {fare_per_mile_chg}, "
        f"leading carrier {carrier} ({share} share)."
    )


def _carrier_fact_line(c: dict) -> str:
    """One data line for a single carrier rollup - reused across every
    carrier-based section sent to Claude."""
    return (
        f"- {c['carrier']}: {c['total_passengers']:,.0f} total passengers, "
        f"average fare ${c['avg_fare']:.2f}, average market share {c['avg_market_share_pct']:.1f}%."
    )


def _quarterly_trend_fact_line(row: dict) -> str:
    """One data line for a single quarter's whole-dataset rollup - same
    style as _market_fact_line/_carrier_fact_line, used for the quarterly
    trend section sent to Claude."""
    growth = "n/a" if row.get("passenger_growth_pct") is None or pd.isna(row.get("passenger_growth_pct")) \
        else f"{row['passenger_growth_pct']:+.1f}%"
    fare_chg = "n/a" if row.get("fare_change_pct") is None or pd.isna(row.get("fare_change_pct")) \
        else f"{row['fare_change_pct']:+.1f}%"
    return (
        f"- {row['year']} Q{row['quarter']}: {row['total_passengers']:,.0f} total passengers, "
        f"{row['total_markets']:,.0f} markets, passenger growth QoQ {growth}, "
        f"average fare ${row['average_fare']:.2f}, fare change QoQ {fare_chg}, "
        f"fare per mile ${row['fare_per_mile']:.4f}."
    )


def _fact_section(title: str, item_lines: list) -> list:
    """One labeled section of the Claude prompt: a title line followed by
    one data line per item, or a placeholder if that category is empty."""
    section = [f"{title}:"]
    section.extend(item_lines if item_lines else ["- (none)"])
    section.append("")
    return section


def _facts_to_prompt(facts: dict) -> str:
    """Render gather_facts() output as compact, unambiguous text for the
    Claude prompt - one line per quarter/market/carrier, only real queried
    numbers. Covers every category gather_facts() produces: the
    whole-dataset quarterly trend (sent first); top markets by volume;
    best/worst passenger growth; best/worst fare movement (company-revenue
    framing: fares up is "best" - more revenue per passenger); and
    best/worst carriers by passenger volume, average fare, and market
    share. The prompt instructions in build_ai_summary_sections() are separate
    and untouched here - this function only controls what data Claude sees,
    not how it's told to write about it."""
    lines = [
        f"Reporting period: {facts['first_year']} Q{facts['first_quarter']} "
        f"through {facts['last_year']} Q{facts['last_quarter']}.",
        "",
    ]

    lines += _fact_section(
        "Whole-dataset quarterly trend (every market combined, one row per quarter)",
        [_quarterly_trend_fact_line(r) for r in facts["quarterly_trend"]],
    )

    lines += _fact_section(
        f"Top {len(facts['markets'])} markets by passenger volume in "
        f"{facts['last_year']} Q{facts['last_quarter']}",
        [_market_fact_line(m) for m in facts["markets"]],
    )
    lines += _fact_section(
        "Best passenger growth (fastest-growing markets)",
        [_market_fact_line(m) for m in facts["best_growth_markets"]],
    )
    lines += _fact_section(
        "Worst passenger growth (steepest declines)",
        [_market_fact_line(m) for m in facts["worst_growth_markets"]],
    )
    lines += _fact_section(
        "Best fare movement (largest fare increases - more revenue per passenger)",
        [_market_fact_line(m) for m in facts["best_fare_markets"]],
    )
    lines += _fact_section(
        "Worst fare movement (largest fare decreases - less revenue per passenger)",
        [_market_fact_line(m) for m in facts["worst_fare_markets"]],
    )
    lines += _fact_section(
        "Top carriers by total passenger volume",
        [_carrier_fact_line(c) for c in facts["best_carrier_passengers"]],
    )
    lines += _fact_section(
        "Bottom carriers by total passenger volume",
        [_carrier_fact_line(c) for c in facts["worst_carrier_passengers"]],
    )
    lines += _fact_section(
        "Top carriers by average fare",
        [_carrier_fact_line(c) for c in facts["best_carrier_fare"]],
    )
    lines += _fact_section(
        "Bottom carriers by average fare",
        [_carrier_fact_line(c) for c in facts["worst_carrier_fare"]],
    )
    lines += _fact_section(
        "Top carriers by average market share",
        [_carrier_fact_line(c) for c in facts["best_carrier_share"]],
    )
    lines += _fact_section(
        "Bottom carriers by average market share",
        [_carrier_fact_line(c) for c in facts["worst_carrier_share"]],
    )

    return "\n".join(lines).rstrip()


def _section_facts_and_instruction(title: str, facts: dict) -> tuple:
    """Return (facts_text, instruction) for one section's Claude call - only
    the facts relevant to that section (tighter context, no topic-blending),
    plus what that blurb should focus on. "Overall Consensus" is the one
    exception - it needs the full picture, not one table's slice of it."""
    if title == "Overall Consensus":
        return _facts_to_prompt(facts), (
            "Give a short overall consensus on the market this quarter, based only on "
            "the facts below, not tied to any one specific table."
        )

    market_sections = {
        "Top Markets by Volume": ("markets", "the standout market(s) by passenger volume"),
        "Best Passenger Growth": ("best_growth_markets", "the fastest-growing market(s)"),
        "Worst Passenger Growth": ("worst_growth_markets", "the steepest-declining market(s)"),
        "Best Fare Movement": (
            "best_fare_markets",
            "the largest fare increases (a fare increase is positive - more revenue per passenger)",
        ),
        "Worst Fare Movement": (
            "worst_fare_markets",
            "the largest fare decreases (a fare decrease is negative - less revenue per passenger)",
        ),
    }
    carrier_sections = {
        "Top Carriers by Passengers": ("best_carrier_passengers", "the carrier(s) with the highest passenger volume"),
        "Bottom Carriers by Passengers": ("worst_carrier_passengers", "the carrier(s) with the lowest passenger volume"),
        "Top Carriers by Average Fare": ("best_carrier_fare", "the carrier(s) with the highest average fare"),
        "Bottom Carriers by Average Fare": ("worst_carrier_fare", "the carrier(s) with the lowest average fare"),
        "Top Carriers by Market Share": ("best_carrier_share", "the carrier(s) with the highest average market share"),
        "Bottom Carriers by Market Share": ("worst_carrier_share", "the carrier(s) with the lowest average market share"),
    }

    if title == "Quarterly Trends":
        lines = [_quarterly_trend_fact_line(r) for r in facts["quarterly_trend"]]
        instruction = "Compare the most recent quarter to the previous quarter, not a quarter-by-quarter recap."
    elif title in market_sections:
        facts_key, what = market_sections[title]
        lines = [_market_fact_line(m) for m in facts[facts_key]]
        instruction = f"Highlight {what}, calling them out by name."
    elif title in carrier_sections:
        facts_key, what = carrier_sections[title]
        lines = [_carrier_fact_line(c) for c in facts[facts_key]]
        instruction = f"Highlight {what}, calling them out by name."
    else:
        raise ValueError(f"Unknown section title: {title}")

    facts_text = "\n".join(lines) if lines else "(no data available for this section this quarter)"
    return facts_text, instruction


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
        "Using ONLY the facts below - do not invent any numbers, markets, or carriers "
        f"not listed - write the blurb for the \"{title}\" section. {instruction}\n\n"
        "This is written from the company's revenue perspective, not the traveler's: "
        "a fare increase is a positive (more revenue per passenger), and a fare "
        "decrease is a negative (less revenue per passenger).\n\n"
        f"{facts_text}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def build_ai_summary_sections(conn, top_n: int = 5, model: str = "claude-sonnet-5") -> dict:
    """Claude engine: makes one Claude API call per section (see
    SECTION_ORDER), each returning plain text directly as that section's
    blurb - no JSON, so there's nothing to parse or break. Each call only
    sees the facts relevant to its own section. Each call fails
    independently: if one section's call fails for any reason (network,
    rate limit, etc.), only that section falls back to the template
    engine's blurb for it - printing a warning first - rather than the
    whole report falling back. Requires ANTHROPIC_API_KEY and
    `pip install anthropic`."""
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
        try:
            sections[title] = _call_claude_for_section(client, model, title, facts)
        except Exception as e:
            print(f"WARNING: Claude call for '{title}' failed ({e}). Using template blurb instead.", file=sys.stderr)
            sections[title] = template_fallback[title]
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
    the 13 per-section calls) are handled inside build_ai_summary_sections()
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
    parser.add_argument("--top", type=int, default=5, help="Number of top markets to summarize (default: 5)")
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
        print(sections.get(title, ""))
        print()


if __name__ == "__main__":
    main()
