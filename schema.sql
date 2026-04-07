-- LivroTrack Database Schema
-- PostgreSQL

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Books table
CREATE TABLE IF NOT EXISTS books (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  asin VARCHAR(20) UNIQUE NOT NULL,
  title TEXT NOT NULL,
  author TEXT,
  publisher TEXT,
  isbn TEXT,
  cover_url TEXT,
  amazon_url TEXT NOT NULL,
  category TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Price history table (core of the system)
CREATE TABLE IF NOT EXISTS price_history (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  book_id UUID NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  price NUMERIC(10,2) NOT NULL,
  original_price NUMERIC(10,2),        -- price before discount
  discount_pct INTEGER,                -- % discount if any
  in_stock BOOLEAN DEFAULT TRUE,
  scraped_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast lookups
CREATE INDEX idx_price_history_book_id ON price_history(book_id);
CREATE INDEX idx_price_history_scraped_at ON price_history(scraped_at DESC);

-- Price alerts
CREATE TABLE IF NOT EXISTS price_alerts (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  book_id UUID NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  email VARCHAR(255) NOT NULL,
  target_price NUMERIC(10,2) NOT NULL,
  triggered BOOLEAN DEFAULT FALSE,
  triggered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_alerts_book_id ON price_alerts(book_id);
CREATE INDEX idx_alerts_triggered ON price_alerts(triggered) WHERE triggered = FALSE;

-- View: latest price per book
CREATE OR REPLACE VIEW book_latest_price AS
SELECT DISTINCT ON (ph.book_id)
  b.id,
  b.asin,
  b.title,
  b.author,
  b.cover_url,
  b.amazon_url,
  ph.price AS current_price,
  ph.original_price,
  ph.discount_pct,
  ph.in_stock,
  ph.scraped_at AS last_checked
FROM books b
JOIN price_history ph ON b.id = ph.book_id
ORDER BY ph.book_id, ph.scraped_at DESC;

-- View: price stats per book (last 365 days)
CREATE OR REPLACE VIEW book_price_stats AS
SELECT
  book_id,
  MIN(price) AS min_price,
  MAX(price) AS max_price,
  ROUND(AVG(price)::numeric, 2) AS avg_price,
  COUNT(*) AS total_records,
  MIN(scraped_at) AS tracking_since
FROM price_history
WHERE scraped_at >= NOW() - INTERVAL '365 days'
GROUP BY book_id;
