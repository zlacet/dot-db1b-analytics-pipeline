-- =====================================================================
-- Airline Price Variance Platform — Database Schema
-- Source: DOT Origin and Destination Survey (DB1B) Market Data
-- =====================================================================

-- ---------------------------------------------------------------------
-- raw_airline_data
-- Purpose: store original DOT DB1BMarket records (one row per itinerary
-- market observation) as loaded by pipeline/ingest.py.
-- Uniqueness key: (itin_id, mkt_id) — a single itinerary can span
-- multiple market records.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_airline_data (
    itin_id                 BIGINT          NOT NULL,
    mkt_id                  BIGINT          NOT NULL,
    year                    SMALLINT        NOT NULL,
    quarter                 SMALLINT        NOT NULL,
    origin                  VARCHAR(5)      NOT NULL,
    dest                    VARCHAR(5)      NOT NULL,
    origin_city_market_id   INTEGER         NOT NULL,
    dest_city_market_id     INTEGER         NOT NULL,
    op_carrier              VARCHAR(5)      NOT NULL,
    passengers              NUMERIC(12, 2)  NOT NULL,
    mkt_fare                NUMERIC(12, 2)  NOT NULL,
    mkt_miles_flown         NUMERIC(12, 2)  NOT NULL,
    CONSTRAINT pk_raw_airline_data PRIMARY KEY (itin_id, mkt_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_year_quarter
    ON raw_airline_data (year, quarter);

-- Note: no indexes on origin_city_market_id/dest_city_market_id or
-- op_carrier - nothing in the pipeline queries raw_airline_data by those
-- columns (routes are built from origin/dest airport codes, and carrier
-- grouping happens after the data's already reduced to a temp table in
-- transform.py), so those indexes would only cost write time on every
-- ingest with no query ever benefiting from them.

-- ---------------------------------------------------------------------
-- route_summary
-- Purpose: route-level performance analysis.
-- Granularity: route + quarter.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_summary (
    route                   VARCHAR(20)     NOT NULL,
    year                    SMALLINT        NOT NULL,
    quarter                 SMALLINT        NOT NULL,
    passengers               NUMERIC(14, 2) NOT NULL,
    passenger_growth_pct     NUMERIC(14, 4),
    average_price             NUMERIC(12, 2) NOT NULL,
    price_per_mile            NUMERIC(10, 4) NOT NULL,
    price_change_pct          NUMERIC(14, 4),
    CONSTRAINT pk_route_summary PRIMARY KEY (route, year, quarter)
);

CREATE INDEX IF NOT EXISTS idx_route_summary_year_quarter
    ON route_summary (year, quarter);


-- ---------------------------------------------------------------------
-- carrier_summary
-- Purpose: carrier performance analysis.
-- Granularity: route + carrier + quarter.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS carrier_summary (
    route                   VARCHAR(20)     NOT NULL,
    carrier                 VARCHAR(5)      NOT NULL,
    year                    SMALLINT        NOT NULL,
    quarter                 SMALLINT        NOT NULL,
    passengers              NUMERIC(14, 2)  NOT NULL,
    average_price            NUMERIC(12, 2)  NOT NULL,
    price_per_mile           NUMERIC(10, 4)  NOT NULL,
    route_share_pct         NUMERIC(9, 4)   NOT NULL,
    CONSTRAINT pk_carrier_summary PRIMARY KEY (route, carrier, year, quarter)
);

CREATE INDEX IF NOT EXISTS idx_carrier_summary_year_quarter
    ON carrier_summary (year, quarter);

-- Note: no index on carrier alone - every query against this table
-- filters by (year, quarter) and groups by carrier afterward; nothing
-- ever looks up by carrier value on its own, so an index on just that
-- column would only cost write time with no query ever using it.


-- ---------------------------------------------------------------------
-- price_variance_summary
-- Purpose: track quarter-over-quarter price movement per route.
-- Granularity: route + quarter (current quarter, compared to previous).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_variance_summary (
    route                       VARCHAR(20)     NOT NULL,
    year                        SMALLINT        NOT NULL,
    quarter                     SMALLINT        NOT NULL,
    current_average_price        NUMERIC(12, 2)  NOT NULL,
    previous_average_price       NUMERIC(12, 2),
    price_change_pct             NUMERIC(14, 4),
    current_price_per_mile       NUMERIC(10, 4)  NOT NULL,
    previous_price_per_mile      NUMERIC(10, 4),
    price_per_mile_change_pct    NUMERIC(14, 4),
    CONSTRAINT pk_price_variance_summary PRIMARY KEY (route, year, quarter)
);

CREATE INDEX IF NOT EXISTS idx_price_variance_year_quarter
    ON price_variance_summary (year, quarter);


-- ---------------------------------------------------------------------
-- ingest_log
-- Purpose: track which quarterly source files have already been fully
-- ingested, so re-running ingest.py after adding a new quarter only
-- processes the new file(s) instead of re-reading everything.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_log (
    filename        TEXT            PRIMARY KEY,
    year            SMALLINT        NOT NULL,
    quarter         SMALLINT        NOT NULL,
    rows_loaded     BIGINT          NOT NULL,
    rows_dropped    BIGINT          NOT NULL,
    ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- validated_quarters
-- Purpose: track which (year, quarter) pairs have already passed
-- validate.py's missing-data/integrity checks, so re-running validate.py
-- after a new quarter is ingested only re-checks what's new.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validated_quarters (
    year            SMALLINT        NOT NULL,
    quarter         SMALLINT        NOT NULL,
    validated_at    TIMESTAMPTZ     NOT NULL DEFAULT now(),
    CONSTRAINT pk_validated_quarters PRIMARY KEY (year, quarter)
);
