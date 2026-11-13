import argparse
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(DIR.parent / ".env", override=True)

import db  # noqa: E402

METRIK_RAGAS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall",
                "answer_correctness"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="")
    args = ap.parse_args()
    pola = f"{args.prefix}%" if args.prefix else "%"

    with db.connect_eval() as conn, conn.cursor() as cur:
        print("=== FASE A / retrieval (per konfigurasi x kategori/set) ===")
        cur.execute("""
            SELECT c.name, q.category,
                   count(*) FILTER (WHERE s.metric = 'recall_gold')                    AS n,
                   round(avg(s.value) FILTER (WHERE s.metric = 'recall_gold')::numeric, 3)   AS r_gold,
                   round(avg(s.value) FILTER (WHERE s.metric = 'recall_acuan')::numeric, 3)  AS r_acuan,
                   round(avg(s.value) FILTER (WHERE s.metric = 'presisi_acuan')::numeric, 3) AS presisi,
                   round(avg(s.value) FILTER (WHERE s.metric = 'token_konteks')::numeric, 0) AS token,
                   round(avg(s.value) FILTER (WHERE s.metric = 'skor_teratas_negatif')::numeric, 3) AS top_neg,
                   sum(s.value) FILTER (WHERE s.metric = 'tolak_benar')                AS tolak_benar,
                   count(*) FILTER (WHERE s.metric = 'tolak_benar')                    AS n_negatif
            FROM eval.eval_configs c
            JOIN eval.eval_runs r    ON r.eval_config_id = c.id
            JOIN eval.eval_answers a ON a.eval_run_id = r.id
            JOIN eval.eval_questions q ON q.id = a.eval_question_id
            LEFT JOIN eval.eval_scores s ON s.eval_answer_id = a.id
            WHERE c.name LIKE %s AND a.answer IS NULL
            GROUP BY c.name, q.category
            ORDER BY c.name, q.category
        """, (pola,))
        print(f"{'konfigurasi':14} {'kategori/set':16} {'n':>3} {'r_gold':>7} {'r_acuan':>8} "
              f"{'presisi':>8} {'token':>6} {'top_neg':>8} {'tolak':>7}")
        for r in cur.fetchall():
            tolak = f"{int(r[8] or 0)}/{r[9]}" if r[9] else "-"
            f = lambda x: f"{float(x):.3f}" if x is not None else "  -  "
            print(f"{r[0]:14} {r[1]:16} {r[2]:>3} {f(r[3]):>7} {f(r[4]):>8} {f(r[5]):>8} "
                  f"{f(r[6]):>6} {f(r[7]):>8} {tolak:>7}")

        print("\n=== FASE A / ringkasan penolakan pertanyaan negatif (semua set) ===")
        cur.execute("""
            SELECT c.name, c.top_k, c.threshold,
                   round(avg(s.value) FILTER (WHERE s.metric = 'skor_teratas_negatif')::numeric, 3) AS top_neg,
                   sum(s.value) FILTER (WHERE s.metric = 'tolak_benar') AS tolak_benar,
                   count(*) FILTER (WHERE s.metric = 'tolak_benar')     AS n
            FROM eval.eval_configs c
            JOIN eval.eval_runs r    ON r.eval_config_id = c.id
            JOIN eval.eval_answers a ON a.eval_run_id = r.id
            LEFT JOIN eval.eval_scores s ON s.eval_answer_id = a.id
            WHERE c.name LIKE %s AND a.answer IS NULL
            GROUP BY 1, 2, 3 ORDER BY top_neg
        """, (pola,))
        print(f"{'konfigurasi':14} {'k':>3} {'thr':>5} {'top_neg':>8} {'tolak':>8}")
        for r in cur.fetchall():
            print(f"{r[0]:14} {r[1]:>3} {r[2]:>5.2f} {float(r[3] or 0):>8.3f} "
                  f"{int(r[4] or 0)}/{r[5]:<6}")

        print("\n=== FASE B / metrik RAGAS (LLM judge) per konfigurasi ===")
        cur.execute("""
            SELECT c.name, c.temperature, r.judge_model, count(DISTINCT a.id),
                   NULLIF(count(s.value) FILTER (WHERE s.metric = 'faithfulness'), 0)
            FROM eval.eval_configs c
            JOIN eval.eval_runs r    ON r.eval_config_id = c.id
            JOIN eval.eval_answers a ON a.eval_run_id = r.id
            LEFT JOIN eval.eval_scores s ON s.eval_answer_id = a.id
            WHERE c.name LIKE %s AND a.answer IS NOT NULL
            GROUP BY 1, 2, 3 ORDER BY 1
        """, (pola,))
        baris = cur.fetchall()
        if not baris:
            print("belum ada jawaban tersimpan (jalankan: eval/fase_b_ragas.py --answer)")
        for r in baris:
            print(f"\n{r[0]} (suhu {r[1]}, penilai {r[2]}, {r[3]} jawaban dinilai)")
            cur.execute("""
                SELECT s.metric, round(avg(s.value)::numeric, 3), count(*)
                FROM eval.eval_scores s
                JOIN eval.eval_answers a ON a.id = s.eval_answer_id
                JOIN eval.eval_runs r    ON r.id = a.eval_run_id
                JOIN eval.eval_configs c ON c.id = r.eval_config_id
                WHERE c.name = %s AND s.metric = ANY(%s)
                GROUP BY 1 ORDER BY 1
            """, (r[0], METRIK_RAGAS))
            for m, rata, n in cur.fetchall():
                print(f"    {m:20} {float(rata):>6.3f}  (n={n})")

        # matriks pembanding: satu baris per konfigurasi -> inilah tabel untuk memilih top_k terbaik
        print("\n=== FASE B / matriks pembanding konfigurasi ===")
        cur.execute("""
            SELECT c.name, c.top_k, c.threshold, count(DISTINCT a.id) AS n_jawaban,
                   round(avg(s.value) FILTER (WHERE s.metric = 'faithfulness')::numeric, 3),
                   round(avg(s.value) FILTER (WHERE s.metric = 'answer_relevancy')::numeric, 3),
                   round(avg(s.value) FILTER (WHERE s.metric = 'context_precision')::numeric, 3),
                   round(avg(s.value) FILTER (WHERE s.metric = 'context_recall')::numeric, 3),
                   count(s.value) FILTER (WHERE s.metric = 'answer_relevancy') AS n_ar
            FROM eval.eval_configs c
            JOIN eval.eval_runs r    ON r.eval_config_id = c.id
            JOIN eval.eval_answers a ON a.eval_run_id = r.id
            LEFT JOIN eval.eval_scores s ON s.eval_answer_id = a.id
            WHERE c.name LIKE %s AND a.answer IS NOT NULL
            GROUP BY 1, 2, 3 ORDER BY c.name
        """, (pola,))
        baris = cur.fetchall()
        print(f"{'konfigurasi':14} {'k':>3} {'thr':>5} {'n':>3} {'faithful':>9} "
              f"{'relevancy':>10} {'ctx_prec':>9} {'ctx_recall':>11} {'n_metrik':>8}")
        tampil = lambda x: f"{float(x):.3f}" if x is not None else "-"
        for r in baris:
            print(f"{r[0]:14} {r[1]:>3} {r[2]:>5.2f} {r[3]:>3} {tampil(r[4]):>9} "
                  f"{tampil(r[5]):>10} {tampil(r[6]):>9} {tampil(r[7]):>11} {r[8]:>8}")


if __name__ == "__main__":
    main()
