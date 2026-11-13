import os
import psycopg
from dotenv import load_dotenv

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL", "")
SEARCH_PATH = os.getenv("DB_SEARCH_PATH", "rag,ext,public")
EVAL_DATABASE_URL = os.getenv("EVAL_DATABASE_URL", "")
EVAL_SEARCH_PATH = os.getenv("EVAL_DB_SEARCH_PATH", "eval,rag,ext,public")


def connect_eval():
    """Koneksi role rag_eval: menulis schema eval, membaca rag.* (tanpa hak tulis runtime)."""
    if not EVAL_DATABASE_URL:
        raise RuntimeError("EVAL_DATABASE_URL belum diisi di .env")
    return psycopg.connect(EVAL_DATABASE_URL, options=f"-c search_path={EVAL_SEARCH_PATH}")


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL belum diisi di .env")
    try:
        # search_path dipaksa di sini supaya query tidak perlu menulis nama schema
        return psycopg.connect(DATABASE_URL, options=f"-c search_path={SEARCH_PATH}")
    except psycopg.OperationalError as e:
        raise RuntimeError(
            f"PostgreSQL tidak bisa dihubungi ({e}). Start service-nya: "
            "Laragon -> menu PostgreSQL -> Start."
        ) from e


def vector_literal(vector) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def fetch_candidates(vector, threshold: float) -> list[dict]:
    literal = vector_literal(vector)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, d.file_name, c.content,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE 1 - (c.embedding <=> %s::vector) >= %s
            ORDER BY round((1 - (c.embedding <=> %s::vector))::numeric, 3) DESC,
                     d.file_name COLLATE "C", c.content COLLATE "C"
            """,
            (literal, literal, threshold, literal),
        )
        return [
            {"chunk_id": r[0], "source": r[1], "text": r[2], "score": float(r[3])}
            for r in cur.fetchall()
        ]


def log_retrieval(question: str, results: list[dict], latency_ms: int,
                  top_k: int, threshold: float) -> None:
    # best effort: gagal mencatat tidak boleh menggagalkan jawaban
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO retrieval_logs (question, result_count, top_score, latency_ms,"
                " top_k, threshold, chunk_ids) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    question[:500],
                    len(results),
                    results[0]["score"] if results else None,
                    latency_ms,
                    top_k,
                    threshold,
                    [r["chunk_id"] for r in results],
                ),
            )
    except Exception as e:
        print(f"[peringatan] gagal mencatat retrieval_logs: {e}")


def upsert_document(file_name: str, sha256: str, byte_size: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (file_name, sha256, byte_size)
            VALUES (%s, %s, %s)
            ON CONFLICT (file_name)
            DO UPDATE SET sha256 = EXCLUDED.sha256, byte_size = EXCLUDED.byte_size,
                          indexed_at = now()
            RETURNING id
            """,
            (file_name, sha256, byte_size),
        )
        return cur.fetchone()[0]


def save_batch(rows: list[dict], document_id: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (document_id, seq, content, embedding)
            VALUES (%s, %s, %s, %s::vector)
            ON CONFLICT (document_id, seq)
            DO UPDATE SET content = EXCLUDED.content, embedding = EXCLUDED.embedding
            """,
            [(document_id, r["seq"], r["text"], vector_literal(r["vector"])) for r in rows],
        )
        cur.execute(
            "UPDATE documents SET chunk_count ="
            " (SELECT count(*) FROM chunks WHERE document_id = %s) WHERE id = %s",
            (document_id, document_id),
        )
        return len(rows)


def start_run(embed_model: str, embed_dim: int, batch_size: int, chunk_count: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_runs (embed_model, embed_dim, batch_size, chunk_count)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (embed_model, embed_dim, batch_size, chunk_count),
        )
        return cur.fetchone()[0]


def update_run(run_id: int, chunks_done: int) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE pipeline_runs SET chunks_done = %s WHERE id = %s", (chunks_done, run_id))


def finish_run(run_id: int, status: str, chunk_count: int, notes: str | None = None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pipeline_runs SET finished_at = now(), status = %s, chunk_count = %s,"
            " notes = %s WHERE id = %s",
            (status, chunk_count, notes, run_id),
        )


def document_state() -> dict:
    """{file_name: {id, sha256, chunks}} untuk memutuskan berkas mana yang perlu di-embed ulang."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT d.file_name, d.id, d.sha256, count(c.id)"
            " FROM documents d LEFT JOIN chunks c ON c.document_id = d.id"
            " GROUP BY 1, 2, 3"
        )
        return {r[0]: {"id": r[1], "sha256": r[2], "chunks": r[3]} for r in cur.fetchall()}


def delete_chunks(document_id: int) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        return cur.rowcount


def delete_document(document_id: int) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def stats() -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM chunks), (SELECT count(*) FROM documents),"
            " (SELECT count(DISTINCT content_md5) FROM chunks)"
        )
        chunks, documents, unique_texts = cur.fetchone()
        return {"chunks": chunks, "documents": documents, "unique_texts": unique_texts}
