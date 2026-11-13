import argparse
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(DIR.parent / ".env", override=True)

import db  # noqa: E402


def persentil(nilai: list[float], p: float) -> float | None:
    if not nilai:
        return None
    urut = sorted(nilai)
    pos = (len(urut) - 1) * p
    bawah, atas = int(pos), min(int(pos) + 1, len(urut) - 1)
    return urut[bawah] + (urut[atas] - urut[bawah]) * (pos - bawah)


def main() -> None:
    ap = argparse.ArgumentParser(description="Ringkasan pemakaian harian dari rag.retrieval_logs")
    ap.add_argument("--hari", type=int, default=7, help="rentang hari ke belakang (default 7)")
    ap.add_argument("--rendah", type=int, default=10, help="banyak pertanyaan skor terendah yang ditampilkan")
    args = ap.parse_args()

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT requested_at::date AS hari, count(*) AS n,
                   count(*) FILTER (WHERE result_count = 0) AS kosong,
                   round(avg(top_score)::numeric, 3) AS skor,
                   round(avg(latency_ms)::numeric, 0) AS latensi
            FROM rag.retrieval_logs
            WHERE requested_at >= now() - make_interval(days => %s)
            GROUP BY 1 ORDER BY 1 DESC
        """, (args.hari,))
        per_hari = cur.fetchall()

        cur.execute("""
            SELECT count(*), count(*) FILTER (WHERE result_count = 0),
                   coalesce(round(avg(result_count)::numeric, 1), 0),
                   round(avg(top_score)::numeric, 3), round(min(top_score)::numeric, 3),
                   round(max(top_score)::numeric, 3)
            FROM rag.retrieval_logs WHERE requested_at >= now() - make_interval(days => %s)
        """, (args.hari,))
        ringkas = cur.fetchone()
        n, kosong, rerata_hasil, skor_rata, skor_min, skor_max = ringkas if ringkas else (0, 0, 0, None, None, None)

        cur.execute("""SELECT latency_ms FROM rag.retrieval_logs
                       WHERE requested_at >= now() - make_interval(days => %s)
                         AND latency_ms IS NOT NULL""", (args.hari,))
        latensi = [r[0] for r in cur.fetchall()]

        cur.execute("""
            SELECT question, result_count, top_score, latency_ms
            FROM rag.retrieval_logs
            WHERE requested_at >= now() - make_interval(days => %s)
            ORDER BY top_score ASC NULLS FIRST LIMIT %s
        """, (args.hari, args.rendah))
        terendah = cur.fetchall()

    print(f"== Pemakaian {args.hari} hari terakhir (rag.retrieval_logs) ==")
    if not n:
        print("belum ada catatan pada rentang ini")
        return
    print(f"pertanyaan            : {n} ({kosong} tanpa hasil)")
    print(f"potongan per jawaban  : {rerata_hasil}")
    print(f"skor teratas          : rata {skor_rata} | min {skor_min} | max {skor_max}")
    if latensi:
        print(f"latensi               : p50 {persentil(latensi, 0.5):.0f} ms | "
              f"p95 {persentil(latensi, 0.95):.0f} ms | max {max(latensi)} ms")
    print("\nper hari:")
    for hari, jml, kos, skor, lat in per_hari:
        print(f"  {hari}  {jml:>3} pertanyaan  {kos:>2} kosong  skor {skor or '-'}  latensi {lat or '-'} ms")
    print(f"\n{len(terendah)} pertanyaan berskor teratas terendah (kandidat dokumen perlu dilengkapi):")
    for tanya, jml_hasil, skor, lat in terendah:
        nilai = f"{skor:.3f}" if skor is not None else "-"
        print(f"  {nilai:>5}  hasil {jml_hasil:>2}  {tanya[:88]}")


if __name__ == "__main__":
    main()
