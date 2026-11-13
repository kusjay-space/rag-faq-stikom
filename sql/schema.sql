-- Skema RAG FAQ kurikulum (PostgreSQL 14 + pgvector)
-- psql -U postgres -d rag_kurikulum -f sql/schema.sql
-- Prasyarat: CREATE DATABASE rag_kurikulum; CREATE ROLE rag_app LOGIN PASSWORD '...';
-- Tata letak: rag (runtime), eval (evaluasi), ext (pgvector). public sengaja kosong.

BEGIN;

CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS eval;
CREATE SCHEMA IF NOT EXISTS ext;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA ext;

-- sesi DDL ini bekerja di schema rag, ext dipakai untuk tipe vector(768)
SET LOCAL search_path = rag, ext, public;

-- ---------- runtime ----------
CREATE TABLE IF NOT EXISTS documents (
  id           bigserial PRIMARY KEY,
  file_name    text NOT NULL UNIQUE,
  sha256       text NOT NULL,
  byte_size    integer NOT NULL,
  chunk_count  integer NOT NULL DEFAULT 0,
  indexed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
  id             bigserial PRIMARY KEY,
  document_id    bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  seq            integer NOT NULL,
  section_title  text,
  content        text NOT NULL,
  content_md5    text GENERATED ALWAYS AS (md5(content)) STORED,
  embedding      vector(768) NOT NULL,
  content_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, seq)
);

-- content_md5 sengaja tidak unik (teks boilerplate berulang), dedupe dilakukan saat query
CREATE INDEX IF NOT EXISTS chunks_content_md5_idx ON chunks (content_md5);
CREATE INDEX IF NOT EXISTS chunks_document_idx    ON chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw  ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING gin (content_tsv);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id           bigserial PRIMARY KEY,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  status       text NOT NULL DEFAULT 'running',
  embed_model  text NOT NULL,
  embed_dim    integer NOT NULL,
  batch_size   integer NOT NULL,
  chunk_count  integer NOT NULL DEFAULT 0,
  chunks_done  integer NOT NULL DEFAULT 0,
  notes        text
);

CREATE TABLE IF NOT EXISTS retrieval_logs (
  id            bigserial PRIMARY KEY,
  question      text NOT NULL,
  result_count  integer NOT NULL DEFAULT 0,
  top_score     real,
  latency_ms    integer,
  top_k         integer NOT NULL,
  threshold     real NOT NULL,
  chunk_ids     bigint[],
  requested_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS retrieval_logs_requested_at_idx ON retrieval_logs (requested_at DESC);

-- ---------- evaluasi ----------
CREATE TABLE IF NOT EXISTS eval.eval_questions (
  id                 bigserial PRIMARY KEY,
  code               text NOT NULL UNIQUE,
  question           text NOT NULL,
  ground_truth       text,
  category           text NOT NULL,
  reference_sources  text[],
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval.eval_question_chunks (
  eval_question_id bigint NOT NULL REFERENCES eval.eval_questions(id) ON DELETE CASCADE,
  chunk_id         bigint NOT NULL REFERENCES rag.chunks(id) ON DELETE CASCADE,
  is_gold          boolean NOT NULL DEFAULT false,  -- potongan acuan paling relevan (cakupan tertinggi)
  PRIMARY KEY (eval_question_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS eval.eval_configs (
  id              bigserial PRIMARY KEY,
  name            text NOT NULL UNIQUE,
  top_k           integer NOT NULL,
  threshold       real    NOT NULL,
  temperature     real    NOT NULL,
  embed_model     text    NOT NULL,
  llm_model       text    NOT NULL,
  prompt_version  text    NOT NULL DEFAULT 'v1',
  notes           text
);

CREATE TABLE IF NOT EXISTS eval.eval_runs (
  id              bigserial PRIMARY KEY,
  eval_config_id  bigint NOT NULL REFERENCES eval.eval_configs(id),
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  git_commit      text,
  ragas_version   text,
  judge_model     text,
  notes           text
);

CREATE TABLE IF NOT EXISTS eval.eval_answers (
  id                bigserial PRIMARY KEY,
  eval_run_id       bigint NOT NULL REFERENCES eval.eval_runs(id) ON DELETE CASCADE,
  eval_question_id  bigint NOT NULL REFERENCES eval.eval_questions(id),
  answer            text,
  contexts          jsonb NOT NULL,
  sources           jsonb NOT NULL,
  latency_ms        integer,
  UNIQUE (eval_run_id, eval_question_id)
);

CREATE TABLE IF NOT EXISTS eval.eval_scores (
  id              bigserial PRIMARY KEY,
  eval_answer_id  bigint NOT NULL REFERENCES eval.eval_answers(id) ON DELETE CASCADE,
  metric          text NOT NULL,
  value           real,
  UNIQUE (eval_answer_id, metric)
);

CREATE TABLE IF NOT EXISTS eval.eval_retrieval_hits (
  eval_answer_id  bigint NOT NULL REFERENCES eval.eval_answers(id) ON DELETE CASCADE,
  chunk_id        bigint NOT NULL REFERENCES rag.chunks(id),
  rank            integer NOT NULL,
  score           real NOT NULL,
  PRIMARY KEY (eval_answer_id, chunk_id)
);

CREATE OR REPLACE VIEW eval.v_config_comparison AS
SELECT c.name, c.top_k, c.threshold, c.temperature,
       round(avg(s.value) FILTER (WHERE s.metric = 'faithfulness')::numeric, 3)      AS faithfulness,
       round(avg(s.value) FILTER (WHERE s.metric = 'answer_relevancy')::numeric, 3)  AS answer_relevancy,
       round(avg(s.value) FILTER (WHERE s.metric = 'context_precision')::numeric, 3) AS context_precision,
       round(avg(s.value) FILTER (WHERE s.metric = 'context_recall')::numeric, 3)    AS context_recall,
       round(avg(a.latency_ms))::int                                                 AS latency_ms
FROM eval.eval_configs c
JOIN eval.eval_runs r    ON r.eval_config_id = c.id
JOIN eval.eval_answers a ON a.eval_run_id = r.id
LEFT JOIN eval.eval_scores s ON s.eval_answer_id = a.id
GROUP BY c.id, c.name, c.top_k, c.threshold, c.temperature
ORDER BY faithfulness DESC NULLS LAST;

-- ---------- hak akses ----------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_eval') THEN
    CREATE ROLE rag_eval LOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA rag, ext TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rag TO rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rag TO rag_app;

GRANT USAGE ON SCHEMA eval, rag TO rag_eval;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA eval TO rag_eval;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA eval TO rag_eval;
GRANT SELECT ON rag.documents, rag.chunks TO rag_eval;

ALTER TABLE rag.documents      OWNER TO rag_app;
ALTER TABLE rag.chunks         OWNER TO rag_app;
ALTER TABLE rag.pipeline_runs  OWNER TO rag_app;
ALTER TABLE rag.retrieval_logs OWNER TO rag_app;

ALTER TABLE eval.eval_questions       OWNER TO rag_eval;
ALTER TABLE eval.eval_question_chunks OWNER TO rag_eval;
ALTER TABLE eval.eval_configs         OWNER TO rag_eval;
ALTER TABLE eval.eval_runs            OWNER TO rag_eval;
ALTER TABLE eval.eval_answers         OWNER TO rag_eval;
ALTER TABLE eval.eval_scores          OWNER TO rag_eval;
ALTER TABLE eval.eval_retrieval_hits  OWNER TO rag_eval;
ALTER VIEW  eval.v_config_comparison  OWNER TO rag_eval;

ALTER ROLE rag_app  SET search_path = rag, ext, public;
ALTER ROLE rag_eval SET search_path = eval, rag, ext, public;

COMMIT;

-- Isi datanya: python ingest.py   (dari data/docs-kurikulum, pakai kuota embedding)
