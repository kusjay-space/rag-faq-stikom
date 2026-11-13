from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(DIR.parent / ".env", override=True)

import db  # noqa: E402

METRIK = [m.strip() for m in os.getenv("EVAL_METRIK", "").split(",") if m.strip()] or \
         ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
NAMA = {"parafrase": "parafrase", "sintesis": "sintesis", "negatif": "negatif",
        "jebakan": "jebakan", "pembanding": "pembanding"}


def short_category(kategori: str) -> str:
    return kategori.split("/")[0]


def main() -> None:
    config = sys.argv[1] if len(sys.argv) > 1 else "D-k8-t60"
    with db.connect_eval() as conn, conn.cursor() as cur:
        cur.execute("""SELECT q.category, count(*) FROM eval.eval_questions q GROUP BY 1 ORDER BY 1""")
        bank: dict[str, int] = {}
        for kategori, n in cur.fetchall():
            bank[short_category(kategori)] = bank.get(short_category(kategori), 0) + n

        cur.execute("""SELECT r.id, count(a.id) FILTER (WHERE a.answer IS NOT NULL)
                       FROM eval.eval_runs r
                       JOIN eval.eval_configs c ON c.id = r.eval_config_id
                       LEFT JOIN eval.eval_answers a ON a.eval_run_id = r.id
                       WHERE c.name = %s GROUP BY r.id ORDER BY r.id DESC LIMIT 1""", (config,))
        run = cur.fetchone()
        if not run:
            sys.exit(f"belum ada run Fase B untuk {config} — jalankan --answer dulu")
        run_id, terjawab = run

        cur.execute("""SELECT count(*) FROM (
                         SELECT a.id FROM eval.eval_answers a
                         JOIN eval.eval_scores s ON s.eval_answer_id = a.id
                         WHERE a.eval_run_id = %s AND s.metric = ANY(%s) AND s.value IS NOT NULL
                         GROUP BY a.id HAVING count(*) = %s) t""",
                    (run_id, METRIK, len(METRIK)))
        lengkap = cur.fetchone()[0]

        cur.execute("""SELECT q.category, count(DISTINCT a.id)
                       FROM eval.eval_answers a
                       JOIN eval.eval_questions q ON q.id = a.eval_question_id
                       JOIN eval.eval_scores s ON s.eval_answer_id = a.id
                       WHERE a.eval_run_id = %s AND s.metric = ANY(%s) AND s.value IS NOT NULL
                       GROUP BY 1 ORDER BY 1""", (run_id, METRIK))
        per_kategori = cur.fetchall()

        cur.execute("""SELECT q.category, s.metric, avg(s.value), count(s.value)
                       FROM eval.eval_scores s
                       JOIN eval.eval_answers a ON a.id = s.eval_answer_id
                       JOIN eval.eval_questions q ON q.id = a.eval_question_id
                       WHERE a.eval_run_id = %s AND s.metric = ANY(%s)
                       GROUP BY 1, 2 ORDER BY 1, 2""", (run_id, METRIK))
        rata = cur.fetchall()

    print(f"config {config} | run #{run_id} | {terjawab} jawaban tersimpan | {time.strftime('%H:%M:%S')}")
    print(f"soal dengan {len(METRIK)} metrik lengkap: {lengkap}/{terjawab} "
          f"({(100 * lengkap / terjawab if terjawab else 0):.0f}%)")
    print("bank soal                    : " + ", ".join(f"{k}={v}" for k, v in sorted(bank.items())))
    print("sudah ada nilai per kategori : " + ", ".join(
        f"{NAMA.get(short_category(k), short_category(k))}={n}" for k, n in per_kategori) or "-")

    if rata:
        print()
        print(f"{'kategori':12} {'metrik':19} {'rata2':>6} {'n':>3}")
        for kategori, metrik, nilai, n in rata:
            angka = "-" if nilai is None else f"{float(nilai):.3f}"
            print(f"{NAMA.get(kategori.split('/')[0], kategori):12} {metrik:19} {angka:>6} {n:>3}")

    if lengkap < terjawab:
        print(f"\ncatatan: {terjawab - lengkap} soal belum lengkap. Penilai menulis hasil per potongan "
              f"(4 soal), jadi angka baru muncul tiap 1-3 menit. Jangan jalankan dua proses penilai sekaligus.")


if __name__ == "__main__":
    main()
