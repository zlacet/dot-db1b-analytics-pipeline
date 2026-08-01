# DOT DB1B Analysis Pipeline

Built on the DOT Origin and Destination Survey (DB1B) Market Data, this end-to-end airline market analytics platform runs a Python ETL pipeline that ingests quarterly DB1BMarket file(s) into PostgreSQL, computes market demand, price, and carrier competition metrics, and surfaces them through generated Excel/PDF reports (including AI-written commentary under each table, by Claude) and a Tableau dashboard, ready to run again each time a new quarter is released.

## Why this project

Every quarter, the Department of Transportation releases tens of millions of raw ticket records through its DB1B survey, a wealth of data with no analysis attached. Extracting meaningful market signals from it typically means re-running the same cleaning, validation, and analysis by hand each quarter.

This project replaces that manual process with an automated pipeline: it ingests each quarterly DB1B filing, validates and cleans the data, and computes the price and demand metrics needed to surface meaningful market movement, consistently, every quarter, without manual rework.

## How it works

1. **Prerequisites.** Python 3.10+ and PostgreSQL running locally. Anthropic API key optional (only for the AI-written narrative).
2. **Get the code.** Clone or download this repository, then open a terminal (Command Prompt/PowerShell on Windows, Terminal on Mac/Linux) in the project folder.
3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt --break-system-packages
   ```

4. **(Optional) Add your API key and DB config:**

   ```bash
   cp .env.example .env
   ```

   Fill in `ANTHROPIC_API_KEY` and your real `PG*` values. If you skip this step, the narrative falls back to a deterministic template instead of Claude. `.env` is gitignored; only `.env.example` is tracked.

5. **Create the tables:**

   ```bash
   python3 database/create_tables.py
   ```

6. **Download the latest quarterly DOT DB1BMarket CSV from [transtats.bts.gov](https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FHK&QO_fu146_anzr=b4vtv0+n0q+Qr56v0n6v10+f748rB) and drop it into the raw folder.**

7. **Rename the file to match the expected format** — add a "Q" before the quarter number (e.g. `Origin_and_Destination_Survey_DB1BMarket_2025_Q2.csv`). This is a manual step; the pipeline won't recognize the file otherwise.

8. **Run one command:** `python3 pipeline/main.py`. This single command runs the full pipeline automatically, in order:
   - **Ingest** (`pipeline/ingest.py`): Loads the CSV into the database, skipping files already processed.
   - **Validate** (`pipeline/validate.py`): Checks the data for missing values, bad records, and duplicates before it's trusted for analysis.
   - **Transform** (`pipeline/transform.py`): Cleans the data and computes price, passenger, and market share metrics by market and carrier.
   - **Load** (`pipeline/load.py`): Writes the computed metrics into the analytics tables.
   - **Reports** (`reports/generate_reports.py`): Generates the quarterly Excel and PDF reports.
   - **AI Summary** (`ai_layer/summarize.py`): Called by the Reports step to write short commentary for each table.

**Optional:** To regenerate the report without rerunning the full pipeline, run `python3 reports/generate_reports.py` independently.

## Sample Output

Latest run covers through 2025 Q2.

- [Quarterly Report (xlsx)](https://github.com/zlacet/dot-db1b-analytics-pipeline/blob/main/reports/quarterly_report.xlsx)
- [Executive Summary (pdf)](https://github.com/zlacet/dot-db1b-analytics-pipeline/blob/main/reports/executive_summary.pdf)

## Tableau Dashboard

A companion Tableau dashboard provides interactive exploration of the same data. It's a work in progress:

- **Full DOT DB1B Overview** — built
  - *What's the overall state of the business this quarter?*

  ![Full DOT DB1B Overview](https://github.com/zlacet/dot-db1b-analytics-pipeline/blob/main/tableau/screenshots/Full%20DOT%20DB1B%20Overview.png)

- **Passenger Growth Analysis** — built
  - *Where and when is passenger demand growing or shrinking?*

  ![Passenger Growth Analysis](https://github.com/zlacet/dot-db1b-analytics-pipeline/blob/main/tableau/screenshots/Passenger%20Growth%20Analysis.png)

- **Price Analysis** — planned
  - *Where and when are prices moving, and is pricing power increasing or eroding?*

- **Quarter Analysis** — planned
  - *How does performance vary by season, and is that pattern changing over time?*

- **Carrier Performance** — planned
  - *How are individual carriers performing against each other?*

[Tableau Dashboard](https://github.com/zlacet/dot-db1b-analytics-pipeline/blob/main/tableau/airline_price_analysis.twbx)

## Database Schema

- **raw_airline_data**: original DOT records, one row per (itin_id, mkt_id)
- **market_summary**: market and quarter grain, with passengers, passenger-weighted average price, price per mile, passenger growth %, price change %
- **carrier_summary**: market, carrier, and quarter grain, with passengers, average price, market share %
- **price_variance_summary**: market and quarter grain, current vs. previous quarter price and price-per-mile, with % change for each

## Key Metrics

Markets are defined by airport pair, with both directions combined into one canonical label (e.g. both JFK to LAX and LAX to JFK roll up to `JFK-LAX`).

- **Passenger-weighted average price** = `SUM(MktFare x Passengers) / SUM(Passengers)`
- **Price per mile** = `SUM(MktFare x Passengers) / SUM(MktMilesFlown x Passengers)`
- **Passenger growth %** = `(current quarter passengers minus previous quarter passengers) / previous quarter passengers`
- **Carrier market share** = `carrier passengers / total market passengers`
- **Price change %** = `(current price minus previous price) / previous price`

## Scalability

The pipeline is not limited to a quarterly cadence or to DB1B data. The same ingest, validate, transform, load, and report flow supports weekly, monthly, quarterly, or annual runs, and can be adapted to other datasets, with minor code changes. Additional metrics can be introduced without restructuring the pipeline, and the underlying PostgreSQL database can accommodate significantly more volume than is currently loaded.

## Known limitations

- **No automated tests.** Nothing checks the pipeline's calculations automatically, so mistakes would only show up by running it and checking the output by hand.
- **The auto-run feature only works on Mac.** It uses a Mac-specific tool to watch the raw folder and run the pipeline automatically. On Windows or Linux, you'd need to run it manually or set up your own scheduler.
- **Processing happens one quarter at a time.** If you add several new quarters at once, they're ingested one after another instead of all at once, so it takes longer the more you add.
- **The AI commentary has a fixed length cap per section.** Each table's commentary is a separate Claude API call limited to 300 tokens of output, enough for a short passage, but not something that can be adjusted without editing the code. If any individual call fails, only that section falls back to a deterministic template sentence instead of the whole report failing.

## Future Work

- **Forecast future quarters** using the historical trend data already being computed, predicting next-quarter passenger demand or price movement rather than just reporting on what already happened.
- **Automated anomaly detection**, flagging markets or carriers with statistically unusual price or passenger swings automatically, instead of relying on the fixed passenger-count thresholds.
- **Natural language querying over the data**, letting someone ask a question like "which carrier gained the most share in transcontinental routes" and get an answer generated from the database, rather than reading a fixed report.
- **Cloud deployment**, containerizing the pipeline and moving Postgres to a managed cloud database, so it doesn't depend on a local machine running.

## Data Source

Dataset: DOT Origin and Destination Survey (DB1B) Market Data. DB1B is a 10% sample of airline tickets, so passenger counts in the data are sample counts, not full totals, useful for relative comparisons like growth % and market share. Granularity: one record equals one itinerary market observation. Quarters covered: 2024 Q2, 2024 Q3, 2024 Q4, 2025 Q1, 2025 Q2.

## Tech stack

Python, PostgreSQL, Tableau, Anthropic API (Claude, a large language model).
