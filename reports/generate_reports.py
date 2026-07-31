"""
generate_reports.py

Builds the two files in reports/ from the analytics tables:

    quarterly_report.xlsx   - 3 sheets:
                              1. Quarterly Trend - whole dataset (every
                                 market combined), one row per quarter,
                                 no domain grouping (it's not market-,
                                 fare-, or carrier-specific)
                              2. Market Summary - every market, every
                                 column (passengers, average fare, fare
                                 per mile, passenger growth %, fare
                                 change %, fare-per-mile change %),
                                 sorted by passengers descending, no row
                                 limit.
                              3. Carrier Summary - every carrier, every
                                 column (total passengers, average fare,
                                 average market share %), sorted by total
                                 passengers descending, no row limit.
                              There used to be 3 separate market sheets
                              (by volume / growth / fare movement) and 3
                              separate carrier sheets (by passengers /
                              fare / share) - but market_summary and
                              carrier_summary already carry every metric
                              on every row, so those were the same rows
                              6 times over, just resorted. One sheet per
                              entity now; the PDF still shows best/worst
                              by each individual metric (see below).
    executive_summary.pdf   - title, then a "Quarterly Summary" heading
                              covering the narrative summary (via
                              ai_layer/summarize.py) and the Quarterly
                              Trend table right underneath it, both on the
                              same page. A page break follows, then the
                              remaining 10 sections grouped under three
                              headings - MARKET, FARE, CARRIER - each
                              capped at ai_layer/summarize.py's
                              BEST_WORST_TOP_N (currently 3), matching the
                              narrative. The "Top Markets by Volume" table
                              is the exception, capped at --top instead.
                              These 10 sections are derived in pandas
                              (sort by the relevant metric, then head()/
                              tail()) from the single Market Summary /
                              Carrier Summary dataframes above - no
                              per-metric queries.

Reuses the exact same query functions ai_layer/summarize.py's narrative
engines are built on (get_quarterly_trend, get_top_markets,
get_ranked_carriers), so the report tables and the narrative text can
never disagree on numbers.

Reads directly from market_summary, carrier_summary, and
fare_variance_summary — run this after pipeline/main.py has loaded the
current quarter's data.

Connection settings (same as the rest of the pipeline) can be overridden
with: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Usage:
    python generate_reports.py
    python generate_reports.py --top 15   # pdf's top-markets table shows top 15 instead of 10
"""

import argparse
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: pandas.\nInstall it with: pip install pandas --break-system-packages")

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("Missing dependency: openpyxl.\nInstall it with: pip install openpyxl --break-system-packages")

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError:
    sys.exit("Missing dependency: reportlab.\nInstall it with: pip install reportlab --break-system-packages")

REPORTS_DIR = Path(__file__).resolve().parent
XLSX_FILE = REPORTS_DIR / "quarterly_report.xlsx"
PDF_FILE = REPORTS_DIR / "executive_summary.pdf"

# Title printed at the top of executive_summary.pdf.
REPORT_TITLE = "Quarterly Airline Fare & Demand Report"

# Make ai_layer/summarize.py importable - reused for both the narrative
# text AND the ranking queries themselves (get_quarterly_trend,
# get_top_markets, get_ranked_markets, get_ranked_carriers), so report
# tables and the narrative can never disagree on numbers.
sys.path.insert(0, str(REPORTS_DIR.parent / "ai_layer"))
import summarize  # noqa: E402

sys.path.insert(0, str(REPORTS_DIR.parent / "database"))
from db import get_connection  # noqa: E402


# Which float-formatted columns each table shape needs.
QUARTERLY_TREND_FLOAT_COLS = [
    "total_passengers", "total_markets", "average_fare", "fare_per_mile",
    "passenger_growth_pct", "fare_change_pct",
]
MARKET_FLOAT_COLS = [
    "passengers", "average_fare", "fare_per_mile",
    "passenger_growth_pct", "fare_change_pct", "fare_per_mile_change_pct",
]
CARRIER_FLOAT_COLS = ["total_passengers", "avg_fare", "avg_market_share_pct"]

# Domain groupings for the PDF: heading -> ordered list of section titles.
# "Quarterly Trend" isn't in here - it's printed on its own, before these.
DOMAIN_GROUPS = [
    ("MARKET", ["Top Markets by Volume", "Best Passenger Growth", "Worst Passenger Growth"]),
    ("FARE", ["Best Fare Movement", "Worst Fare Movement"]),
    (
        "CARRIER",
        [
            "Top Carriers by Passengers",
            "Bottom Carriers by Passengers",
            "Top Carriers by Average Fare",
            "Bottom Carriers by Average Fare",
            "Top Carriers by Market Share",
            "Bottom Carriers by Market Share",
        ],
    ),
]
# MARKET and FARE sections both come from get_ranked_markets() (market-shaped
# columns); only CARRIER sections come from get_ranked_carriers().
MARKET_SHAPED_DOMAINS = {"MARKET", "FARE"}


def latest_quarter(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(year * 10 + quarter) FROM market_summary;")
        (key,) = cur.fetchone()
    return key // 10, key % 10


def fetch_base_metrics(conn, year: int, quarter: int) -> dict:
    """Run exactly one query per entity - one for every market, one for
    every carrier - each already carrying every metric column on every
    row. Both the xlsx (the full list as-is) and all 10 of the pdf's
    per-metric best/worst sections (sorted + head()/tail() slices of
    these same two dataframes, see _pdf_sections) are derived from these
    results - no metric is ever queried on its own. Does not include the
    quarterly trend, which is a separate whole-dataset query (see
    get_quarterly_trend)."""
    return {
        "Market Summary": summarize.get_top_markets(conn, year, quarter, top_n=None),
        "Carrier Summary": summarize.get_ranked_carriers(
            conn, year, quarter, "total_passengers", ascending=False, top_n=None
        ),
    }


def _pdf_sections(base_metrics: dict, top_markets_n: int) -> dict:
    """Derive the PDF's 10 domain-grouped sections from the Market Summary
    / Carrier Summary dataframes by sorting on the relevant metric in
    pandas, then head()/tail() slicing - no additional queries. Growth %
    and fare change % sorts drop rows with no value for that column first
    (thin/new routes with no prior-quarter comparison), matching what the
    narrative engines exclude. "Worst" sections use a reversed tail() so
    the single worst performer leads, mirroring how "best" sections lead
    with the single best performer."""
    n = summarize.BEST_WORST_TOP_N

    def best_worst(df, metric):
        ranked = df.dropna(subset=[metric]).sort_values(metric, ascending=False)
        return ranked.head(n), ranked.tail(n).iloc[::-1]

    markets = base_metrics["Market Summary"]  # already sorted by passengers descending
    carriers = base_metrics["Carrier Summary"]  # already sorted by total_passengers descending
    # Same minimum-passenger floors as summarize.py's gather_facts(). For
    # carriers: keeps a carrier with a single low-volume appearance (which
    # can average out to a misleading 100% market share) from ranking as a
    # "top"/"bottom" performer by average fare or average market share. For
    # markets: keeps a market that's still small in absolute terms from
    # ranking as a "top"/"bottom" performer by growth % or fare change %,
    # even if it cleared transform.py's separate prior-quarter floor.
    # Neither filter applies to the by-passengers/by-volume rankings, where
    # showing genuinely small markets/carriers is the point.
    qualifying_markets = markets[markets["passengers"] >= summarize.MIN_MARKET_PASSENGERS]
    qualifying_carriers = carriers[carriers["total_passengers"] >= summarize.MIN_CARRIER_PASSENGERS]

    best_growth, worst_growth = best_worst(qualifying_markets, "passenger_growth_pct")
    best_fare, worst_fare = best_worst(qualifying_markets, "fare_change_pct")
    best_carrier_pax, worst_carrier_pax = best_worst(carriers, "total_passengers")
    best_carrier_fare, worst_carrier_fare = best_worst(qualifying_carriers, "avg_fare")
    best_carrier_share, worst_carrier_share = best_worst(qualifying_carriers, "avg_market_share_pct")

    return {
        "Top Markets by Volume": markets.head(top_markets_n),
        "Best Passenger Growth": best_growth,
        "Worst Passenger Growth": worst_growth,
        "Best Fare Movement": best_fare,
        "Worst Fare Movement": worst_fare,
        "Top Carriers by Passengers": best_carrier_pax,
        "Bottom Carriers by Passengers": worst_carrier_pax,
        "Top Carriers by Average Fare": best_carrier_fare,
        "Bottom Carriers by Average Fare": worst_carrier_fare,
        "Top Carriers by Market Share": best_carrier_share,
        "Bottom Carriers by Market Share": worst_carrier_share,
    }


# ---------------------------------------------------------------------
# quarterly_report.xlsx
# ---------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _write_sheet(wb: Workbook, title: str, df: pd.DataFrame) -> None:
    ws = wb.create_sheet(title=title[:31])  # Excel sheet names are capped at 31 chars
    ws.append(list(df.columns))
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT

    for row in df.itertuples(index=False):
        ws.append(list(row))

    for i, col in enumerate(df.columns, start=1):
        width = max(12, len(str(col)) + 2)
        ws.column_dimensions[get_column_letter(i)].width = width

    ws.freeze_panes = "A2"


def build_xlsx(quarterly_trend_df: pd.DataFrame, base_metrics: dict) -> None:
    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    _write_sheet(wb, "Quarterly Trend", quarterly_trend_df)
    for title, df in base_metrics.items():
        _write_sheet(wb, title, df)

    wb.save(XLSX_FILE)
    sheets = [("Quarterly Trend", quarterly_trend_df), *base_metrics.items()]
    row_counts = ", ".join(f"{title}={len(df):,}" for title, df in sheets)
    print(f"Wrote {XLSX_FILE} ({len(sheets)} sheets, no row limit)\n  {row_counts}")


# ---------------------------------------------------------------------
# executive_summary.pdf
# ---------------------------------------------------------------------

def _df_to_table(df: pd.DataFrame, float_cols=()) -> Table:
    display = df.copy()
    for col in float_cols:
        if col in display.columns:
            display[col] = display[col].map(lambda v: "" if pd.isna(v) else f"{v:,.2f}")
    data = [list(display.columns)] + display.astype(str).values.tolist()

    table = Table(data, repeatRows=1)
    table.hAlign = "LEFT"  # reportlab centers tables by default - keep them left-aligned instead
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
            ]
        )
    )
    # Never split a table mid-row across a page break - push the whole thing
    # onto the next page instead.
    table.splitByRow = 0
    return table


def build_pdf(narrative: str, quarterly_trend_df: pd.DataFrame, sections: dict) -> None:
    doc = SimpleDocTemplate(str(PDF_FILE), pagesize=letter)
    styles = getSampleStyleSheet()
    # Section-level headers (Executive Summary, QUARTERLY TREND, MARKET, FARE,
    # CARRIER) - smaller than the 18pt report Title, bigger than the 10pt
    # table sub-headings below, so all three levels are visually distinct.
    section_heading_style = ParagraphStyle(
        "SectionHeading", parent=styles["Heading1"], fontSize=15, leading=18,
    )
    # Smaller style for per-table titles ("Top Markets by Volume", etc.).
    table_title_style = ParagraphStyle(
        "TableTitle", parent=styles["Heading2"], fontSize=10, leading=12,
    )
    story = [
        Paragraph(REPORT_TITLE, styles["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph("Quarterly Summary", section_heading_style),
        Spacer(1, 0.15 * inch),
    ]

    # The narrative isn't supposed to contain any header lines at all - the
    # prompt tells Claude "no headers" - so there's no special-cased heading
    # detection/styling here anymore. Every line just prints as normal body
    # text (or a bullet), whatever it is.
    for line in narrative.split("\n"):
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 0.08 * inch))
            continue
        if stripped.startswith("="):
            continue
        if line.startswith("- "):
            story.append(Paragraph(line[2:], styles["Bullet"]))
        else:
            story.append(Paragraph(line, styles["Normal"]))

    # The Quarterly Trend table sits right under the written summary, on the
    # same page (it's what "Quarterly Summary" above is titling) - no page
    # break and no separate heading of its own.
    story.append(Spacer(1, 0.2 * inch))
    story.append(
        KeepTogether(
            [
                _df_to_table(quarterly_trend_df, float_cols=QUARTERLY_TREND_FLOAT_COLS),
            ]
        )
    )

    # MARKET / FARE / CARRIER start clean on their own page, after the
    # summary + quarterly table page. The domain heading is bundled into the
    # same KeepTogether block as its first section (title + table) so the
    # domain heading can't get orphaned from every section under it either.
    story.append(PageBreak())
    for domain, section_titles in DOMAIN_GROUPS:
        float_cols = MARKET_FLOAT_COLS if domain in MARKET_SHAPED_DOMAINS else CARRIER_FLOAT_COLS
        for i, title in enumerate(section_titles):
            block = []
            if i == 0:
                block.append(Spacer(1, 0.3 * inch))
                block.append(Paragraph(domain, section_heading_style))
            block.append(Spacer(1, 0.15 * inch))
            block.append(Paragraph(title, table_title_style))
            block.append(_df_to_table(sections[title], float_cols=float_cols))
            story.append(KeepTogether(block))

    doc.build(story)
    print(f"Wrote {PDF_FILE} (quarterly trend + {len(sections)} tables across {len(DOMAIN_GROUPS)} domains)")


def run_reports(top_n: int = 10) -> None:
    """Build quarterly_report.xlsx (3 sheets, every row, no limit) and
    executive_summary.pdf (quarterly trend + 10 capped, domain-grouped
    sections) from whatever is currently in market_summary /
    carrier_summary / fare_variance_summary. Callable directly (e.g. from
    pipeline/main.py) as well as via the CLI."""
    conn = get_connection()
    try:
        year, quarter = latest_quarter(conn)
        print(f"Building reports for {year} Q{quarter}...")

        quarterly_trend_df = summarize.get_quarterly_trend(conn)
        base_metrics = fetch_base_metrics(conn, year, quarter)
        pdf_sections = _pdf_sections(base_metrics, top_markets_n=top_n)

        narrative = summarize.generate_narrative(conn, top_n=top_n)
    finally:
        conn.close()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    build_xlsx(quarterly_trend_df, base_metrics)
    build_pdf(narrative, quarterly_trend_df, pdf_sections)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate quarterly_report.xlsx and executive_summary.pdf")
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Rows in the PDF's 'Top Markets by Volume' table (default: 10). "
        "The xlsx has no row limit on any sheet; every other PDF table is capped at "
        "ai_layer/summarize.py's BEST_WORST_TOP_N (currently 3).",
    )
    args = parser.parse_args()

    run_reports(top_n=args.top)


if __name__ == "__main__":
    main()
