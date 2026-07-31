"""
ingest.py

Reads the four quarterly DB1BMarket CSV files, combines them, selects the
required fields, and bulk-loads them into the raw_airline_data table.

Files are large (multi-GB, millions of rows), so each CSV is streamed in
chunks rather than loaded into memory all at once, and each chunk is bulk
loaded with PostgreSQL COPY (much faster than row-by-row INSERT).

Loading is idempotent: re-running ingest.py after a partial or repeat run
will not create duplicate rows, because each chunk is first copied into a
temporary staging table, then merged into raw_airline_data with
ON CONFLICT (itin_id, mkt_id) DO NOTHING.

Re-runs are also incremental: an ingest_log table records every filename
that has been fully ingested. On each run, files already in ingest_log are
skipped entirely (no re-reading multi-GB files that haven't changed) - so
adding one new quarter's CSV to data/raw/ and re-running only processes
that new file.

Connection settings (same as create_tables.py) can be overridden with:
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

The raw data directory can be overridden with:
    RAW_DATA_DIR

Usage:
    python ingest.py
"""

import io
import os
import re
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency: pandas.\nInstall it with: pip install pandas --break-system-packages")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "database"))
from db import get_connection

DEFAULT_RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
RAW_DIR = Path(os.environ.get("RAW_DATA_DIR", DEFAULT_RAW_DIR))

CHUNK_SIZE = 200_000

# Map DB1BMarket CSV column names -> raw_airline_data column names.
# Column order here defines the load order into the database.
COLUMN_MAP = {
    "ItinID": "itin_id",
    "MktID": "mkt_id",
    "Year": "year",
    "Quarter": "quarter",
    "Origin": "origin",
    "Dest": "dest",
    "OriginCityMarketID": "origin_city_market_id",
    "DestCityMarketID": "dest_city_market_id",
    "OpCarrier": "op_carrier",
    "Passengers": "passengers",
    "MktFare": "mkt_fare",
    "MktMilesFlown": "mkt_miles_flown",
}

DB_COLUMNS = list(COLUMN_MAP.values())

# Nullable dtypes on read so a stray missing value doesn't blow up parsing;
# rows missing any required field are dropped before load (counted + reported).
READ_DTYPES = {
    "ItinID": "Int64",
    "MktID": "Int64",
    "Year": "Int64",
    "Quarter": "Int64",
    "Origin": "string",
    "Dest": "string",
    "OriginCityMarketID": "Int64",
    "DestCityMarketID": "Int64",
    "OpCarrier": "string",
    "Passengers": "float64",
    "MktFare": "float64",
    "MktMilesFlown": "float64",
}

INT_DB_COLUMNS = [
    "itin_id",
    "mkt_id",
    "year",
    "quarter",
    "origin_city_market_id",
    "dest_city_market_id",
]

FILENAME_PATTERN = re.compile(r"DB1BMarket_(\d{4})_Q(\d)\.csv$")


def discover_files():
    """Return the sorted list of quarterly CSVs found in the raw data dir."""
    files = sorted(RAW_DIR.glob("Origin_and_Destination_Survey_DB1BMarket_*.csv"))
    if not files:
        sys.exit(f"No DB1BMarket CSV files found in {RAW_DIR}")
    return files


def parse_year_quarter(file_path: Path) -> tuple[int, int]:
    """Extract (year, quarter) from a DB1BMarket filename, e.g.
    Origin_and_Destination_Survey_DB1BMarket_2025_Q2.csv -> (2025, 2)."""
    match = FILENAME_PATTERN.search(file_path.name)
    if not match:
        sys.exit(f"Could not parse year/quarter from filename: {file_path.name}")
    return int(match.group(1)), int(match.group(2))


def ensure_ingest_log(cur) -> None:
    """Create the ingest_log tracking table if it doesn't already exist
    (also defined in schema.sql; created here too so ingest.py is safe to
    run standalone)."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_log (
            filename        TEXT            PRIMARY KEY,
            year            SMALLINT        NOT NULL,
            quarter         SMALLINT        NOT NULL,
            rows_loaded     BIGINT          NOT NULL,
            rows_dropped    BIGINT          NOT NULL,
            ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
        );
        """
    )


def get_ingested_filenames(cur) -> set:
    """Return the set of filenames already fully ingested in a prior run."""
    cur.execute("SELECT filename FROM ingest_log;")
    return {row[0] for row in cur.fetchall()}


def record_ingest(cur, file_path: Path, year: int, quarter: int, rows_loaded: int, rows_dropped: int) -> None:
    """Mark a file as fully ingested so future runs skip it."""
    cur.execute(
        """
        INSERT INTO ingest_log (filename, year, quarter, rows_loaded, rows_dropped)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (filename) DO UPDATE SET
            rows_loaded = EXCLUDED.rows_loaded,
            rows_dropped = EXCLUDED.rows_dropped,
            ingested_at = now();
        """,
        (file_path.name, year, quarter, rows_loaded, rows_dropped),
    )


def create_staging_table(cur) -> None:
    """Create a session-scoped temp table shaped like raw_airline_data,
    used as a landing zone for COPY before the ON CONFLICT merge."""
    cur.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS staging_raw (
            itin_id                 BIGINT,
            mkt_id                  BIGINT,
            year                    SMALLINT,
            quarter                 SMALLINT,
            origin                  VARCHAR(5),
            dest                    VARCHAR(5),
            origin_city_market_id   INTEGER,
            dest_city_market_id     INTEGER,
            op_carrier              VARCHAR(5),
            passengers              NUMERIC(12, 2),
            mkt_fare                NUMERIC(12, 2),
            mkt_miles_flown         NUMERIC(12, 2)
        ) ON COMMIT PRESERVE ROWS;
        """
    )


def clean_chunk(chunk: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Rename columns to DB names, drop rows missing any required field,
    and cast to the right dtypes for COPY. Returns (clean_chunk, dropped_count)."""
    chunk = chunk.rename(columns=COLUMN_MAP)[DB_COLUMNS]

    before = len(chunk)
    chunk = chunk.dropna(subset=DB_COLUMNS)
    dropped = before - len(chunk)

    for col in INT_DB_COLUMNS:
        chunk[col] = chunk[col].astype("int64")

    return chunk, dropped


def load_chunk(cur, chunk: pd.DataFrame) -> None:
    """COPY a cleaned chunk into staging_raw, then merge into
    raw_airline_data, skipping any (itin_id, mkt_id) already present."""
    buffer = io.StringIO()
    chunk.to_csv(buffer, index=False, header=False)
    buffer.seek(0)

    cur.execute("TRUNCATE staging_raw;")
    cur.copy_expert(
        f"COPY staging_raw ({', '.join(DB_COLUMNS)}) FROM STDIN WITH (FORMAT csv)",
        buffer,
    )
    cur.execute(
        f"""
        INSERT INTO raw_airline_data ({', '.join(DB_COLUMNS)})
        SELECT {', '.join(DB_COLUMNS)} FROM staging_raw
        ON CONFLICT (itin_id, mkt_id) DO NOTHING;
        """
    )


def ingest_file(conn, file_path: Path) -> tuple[int, int]:
    """Stream one quarterly CSV in chunks, loading each into the database.
    Returns (rows_loaded, rows_dropped) for this file."""
    total_read = 0
    total_dropped = 0

    reader = pd.read_csv(
        file_path,
        usecols=list(COLUMN_MAP.keys()),
        dtype=READ_DTYPES,
        chunksize=CHUNK_SIZE,
    )

    with conn.cursor() as cur:
        create_staging_table(cur)
        conn.commit()

        for chunk_num, raw_chunk in enumerate(reader, start=1):
            total_read += len(raw_chunk)
            clean, dropped = clean_chunk(raw_chunk)
            total_dropped += dropped

            if not clean.empty:
                load_chunk(cur, clean)
                conn.commit()

            print(
                f"  {file_path.name}: chunk {chunk_num} "
                f"({len(raw_chunk):,} read, {dropped:,} dropped for missing fields)"
            )

    return total_read, total_dropped


def main() -> None:
    files = discover_files()
    print(f"Found {len(files)} quarterly file(s) in {RAW_DIR}")

    conn = get_connection()
    grand_read = 0
    grand_dropped = 0

    try:
        with conn.cursor() as cur:
            ensure_ingest_log(cur)
        conn.commit()

        with conn.cursor() as cur:
            already_ingested = get_ingested_filenames(cur)

        to_process = [f for f in files if f.name not in already_ingested]
        already_done = [f for f in files if f.name in already_ingested]

        if already_done:
            print(
                f"Skipping {len(already_done)} already-ingested file(s): "
                f"{', '.join(f.name for f in already_done)}"
            )

        if not to_process:
            print("No new files to ingest.")
            return

        print(f"Processing {len(to_process)} new file(s): {', '.join(f.name for f in to_process)}")

        for file_path in to_process:
            print(f"\nIngesting {file_path.name} ...")
            year, quarter = parse_year_quarter(file_path)
            read, dropped = ingest_file(conn, file_path)
            grand_read += read
            grand_dropped += dropped
            print(f"  -> {read:,} rows read, {dropped:,} dropped")

            with conn.cursor() as cur:
                record_ingest(cur, file_path, year, quarter, read, dropped)
            conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_airline_data;")
            total_in_table = cur.fetchone()[0]

        print("\nIngest complete.")
        print(f"  Files processed this run: {len(to_process)}")
        print(f"  Total rows read this run: {grand_read:,}")
        print(f"  Total rows dropped this run (missing required fields): {grand_dropped:,}")
        print(f"  Total rows now in raw_airline_data: {total_in_table:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
