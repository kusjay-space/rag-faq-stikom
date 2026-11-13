import argparse
import json
import re
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(DIR.parent / ".env", override=True)

import db  # noqa: E402

MAKS_POTONGAN = 4
CAKUPAN_MIN = 0.5  # ambang potongan dianggap acuan: >= 50% dari cakupan terbaik

STOP = {"yang", "untuk", "pada", "dari", "dengan", "atau", "dan", "ada", "di", "ke",
        "itu", "ini", "adalah", "tidak", "bisa", "oleh", "serta", "juga", "dalam"}


def tokenize_words(teks: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", teks.lower()) if len(w) > 2 and w not in STOP}


def load_question_files() -> list[dict]:
    rows = []
    for path in sorted(DIR.glob("pertanyaan*.jsonl")):
        for baris in path.read_text(encoding="utf-8").splitlines():
            if baris.strip():
                rows.append(json.loads(baris))
    return rows


def link_chunks(cur, qid: int, sumber: list[str], jawaban: str) -> list[tuple[int, str, float]]:
    """Potongan acuan untuk satu pertanyaan. Negatif (sumber kosong) sengaja tidak ditautkan."""
    if not sumber:
        return []
    cur.execute(
        "SELECT c.id, d.file_name, c.content FROM rag.chunks c"
        " JOIN rag.documents d ON d.id = c.document_id"
        " WHERE d.file_name = ANY(%s)",
        (sumber,),
    )
    gt = tokenize_words(jawaban)
    if not gt:
        return []
    skor = []
    for cid, nama, isi in cur.fetchall():
        coverage = len(gt & tokenize_words(isi)) / len(gt)
        if coverage > 0:
            skor.append((cid, nama, coverage))
    skor.sort(key=lambda r: (-r[2], r[0]))
    if not skor:
        return []
    batas = skor[0][2] * CAKUPAN_MIN
    return [r for r in skor[:MAKS_POTONGAN] if r[2] >= batas]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hapus", action="store_true", help="kosongkan eval_questions dulu")
    args = ap.parse_args()

    rows = load_question_files()
    print(f"{len(rows)} pertanyaan dari berkas JSONL")

    with db.connect_eval() as conn, conn.cursor() as cur:
        if args.hapus:
            # urutan penting: jawaban -> run -> pertanyaan (FK tanpa ON DELETE CASCADE dari eval_answers)
            cur.execute("DELETE FROM eval.eval_answers")
            print(f"eval_answers dikosongkan ({cur.rowcount} baris, termasuk skor & potongan)")
            cur.execute("DELETE FROM eval.eval_runs")
            print(f"eval_runs dikosongkan ({cur.rowcount} baris)")
            cur.execute("DELETE FROM eval.eval_questions")
            print("eval_questions dikosongkan")

        tanpa_tautan = []
        for r in rows:
            cur.execute(
                """
                INSERT INTO eval.eval_questions
                    (code, question, ground_truth, category, reference_sources)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET question = EXCLUDED.question,
                    ground_truth = EXCLUDED.ground_truth, category = EXCLUDED.category,
                    reference_sources = EXCLUDED.reference_sources
                RETURNING id
                """,
                (r["kode"], r["pertanyaan"], r["jawaban_acuan"],
                 f"{r['kategori']}/{r.get('set', 'setel')}", r["sumber"]),
            )
            qid = cur.fetchone()[0]

            cur.execute("DELETE FROM eval.eval_question_chunks WHERE eval_question_id = %s", (qid,))
            tautan = link_chunks(cur, qid, r["sumber"], r["jawaban_acuan"])
            for urutan, (cid, _, _) in enumerate(tautan):
                cur.execute(
                    "INSERT INTO eval.eval_question_chunks (eval_question_id, chunk_id, is_gold)"
                    " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (qid, cid, urutan == 0),  # urutan 0 = cakupan kata tertinggi
                )
            tanda = " " if tautan else ("-" if not r["sumber"] else "!")
            print(f"  {tanda} {r['kode']:22} {r['kategori']:8} {len(tautan)} potongan acuan")
            if not tautan and r["sumber"]:
                tanpa_tautan.append(r["kode"])

    print(f"\nselesai: {len(rows)} pertanyaan, {len(tanpa_tautan)} tanpa potongan acuan"
          f"{' -> ' + ', '.join(tanpa_tautan) if tanpa_tautan else ''}")


if __name__ == "__main__":
    main()
