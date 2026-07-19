-- Documents that have been fully converted, keyed by content hash + config
-- fingerprint. A hit here means a re-submitted file needs no work at all.
CREATE TABLE IF NOT EXISTS documents (
  doc_key      TEXT PRIMARY KEY,
  file_hash    TEXT NOT NULL,
  config_fp    TEXT NOT NULL,
  total_pages  INTEGER NOT NULL,
  total_chunks INTEGER NOT NULL,
  total_length INTEGER NOT NULL,
  page_size    INTEGER NOT NULL,
  created_at   INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_expires ON documents(expires_at);

CREATE TABLE IF NOT EXISTS jobs (
  job_id           TEXT PRIMARY KEY,
  doc_key          TEXT NOT NULL,
  file_hash        TEXT NOT NULL,
  url              TEXT NOT NULL,
  status           TEXT NOT NULL,   -- queued|running|assembling|done|failed
  total_chunks     INTEGER NOT NULL,
  remaining_chunks INTEGER NOT NULL,
  use_llm          INTEGER NOT NULL,
  error            TEXT,
  error_chunk      INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_doc_key ON jobs(doc_key);

-- This table is the work queue. Claiming is a single atomic UPDATE, and
-- because SQLite serialises writers two workers can never claim the same row.
--
-- Retry, crash recovery, backoff and dead-lettering all fall out of three
-- columns: attempts (incremented at claim time so an OOM-killed worker still
-- burns an attempt), available_at (backoff gate) and lease_expires_at (a lease
-- that stops being renewed is a worker that died).
CREATE TABLE IF NOT EXISTS job_chunks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id           TEXT NOT NULL,
  chunk_index      INTEGER NOT NULL,
  start_page       INTEGER NOT NULL,
  end_page         INTEGER NOT NULL,  -- exclusive; -1 means "the whole file"
  status           TEXT NOT NULL,     -- pending|running|done|failed
  attempts         INTEGER NOT NULL DEFAULT 0,
  available_at     INTEGER NOT NULL,
  lease_owner      TEXT,
  lease_expires_at INTEGER,
  s3_key           TEXT,
  last_error       TEXT,
  updated_at       INTEGER NOT NULL,
  UNIQUE (job_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_claim
  ON job_chunks(status, available_at, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_chunks_job ON job_chunks(job_id, chunk_index);
