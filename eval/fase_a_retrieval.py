import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(DIR.parent / ".env", override=True)

import db  # noqa: E402
import rag  # noqa: E402

CACHE = DIR / ".cache" / "embedding.json"


def cache_vector(q: str) -> tuple[list[float], bool]:
    """Vektor pertanyaan dari cache; True = baru dipanggil ke API."""
    CACHE.parent.mkdir(exist_ok=True)
    isi = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    kunci = hashlib.md5(q.encode("utf-8")).hexdigest()
    if kunci in isi:
        return isi[kunci], False
    vektor = rag.get_embedding(q)
    isi[kunci] = vektor
    CACHE.write_text(json.dumps(isi), encoding="utf-8")
    return vektor, True


def pick_chunks(kandidat: list[dict], threshold: float, top_k: int) -> list[dict]:
    """Sama persis dengan rag.retrieve(): saring threshold lalu buang teks kembar, ambil top_k."""
    seen, hasil = set(), []
    for item in kandidat:
        if item["score"] < threshold:
            continue
        kunci = item["text"].strip()
        if kunci in seen:
            continue
        seen.add(kunci)
        hasil.append(item)
        if len(hasil) == top_k:
            break
    return hasil


def git_short_hash() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=DIR.parent,
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", default="3,5,8,12,15")
    ap.add_argument("--threshold", default="0.50,0.60,0.70")
    ap.add_argument("--limit", type=int, default=0, help="batasi jumlah pertanyaan (uji cepat)")
    ap.add_argument("--suhu", type=float, default=0.0,
                    help="temperature untuk FASE B nanti (FASE A sendiri tidak memakai LLM)")
    ap.add_argument("--jenis", default="", help="hanya kategori tertentu, mis. 'parafrase,sintesis'")
    ap.add_argument("--semua", action="store_true",
                    help="ikutkan juga kategori 'pembanding' (pertanyaan salinan dokumen)")
    ap.add_argument("--tag", default="A")
    args = ap.parse_args()

    daftar_k = [int(x) for x in args.k.split(",")]
    daftar_t = [float(x) for x in args.threshold.split(",")]

    with db.connect_eval() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT q.id, q.code, q.question, q.ground_truth, q.category,
                   coalesce(array_agg(c.chunk_id) FILTER (WHERE c.chunk_id IS NOT NULL), '{}'),
                   coalesce(array_agg(c.chunk_id) FILTER (WHERE c.is_gold), '{}')
            FROM eval.eval_questions q
            LEFT JOIN eval.eval_question_chunks c ON c.eval_question_id = q.id
            GROUP BY q.id ORDER BY q.code
        """)
        soal = [{"id": r[0], "kode": r[1], "q": r[2], "gt": r[3], "kategori": r[4],
                 "acuan": set(r[5]), "gold": set(r[6])} for r in cur.fetchall()]
    # urutan penting: saring kategori DULU, baru --limit dipotong
    # (kalau --limit duluan, potongannya diambil dari seluruh bank lalu filternya bisa kosong)
    if args.jenis:
        pilih = {j.strip() for j in args.jenis.split(",") if j.strip()}
        soal = [s for s in soal if s["kategori"].split("/")[0] in pilih]
        print(f"filter jenis {sorted(pilih)} -> {len(soal)} pertanyaan")
    if not args.jenis and not args.semua:
        # soal 'pembanding' = kalimat dokumen apa adanya; dipakai pembanding saja, bukan uji mutu
        soal = [s for s in soal if s["kategori"].split("/")[0] != "pembanding"]
    if args.limit:
        soal = soal[:args.limit]
    if not soal:
        sys.exit("eval_questions kosong — jalankan: python eval/muat_pertanyaan.py")

    # 1. embed sekali, simpan
    kandidat = {}
    baru = 0
    for s in soal:
        vektor, panggil = cache_vector(s["q"])
        baru += panggil
        t0 = time.perf_counter()
        kandidat[s["kode"]] = db.fetch_candidates(vektor, -1.0)
        s["latensi_sql_ms"] = int((time.perf_counter() - t0) * 1000)
    print(f"{len(soal)} pertanyaan: {baru} embedding baru, {len(soal) - baru} dari cache "
          f"({CACHE.relative_to(DIR.parent)})")
    print(f"kandidat per pertanyaan: rata-rata "
          f"{statistics.mean(len(k) for k in kandidat.values()):.0f} potongan\n")

    ringkas = []
    with db.connect_eval() as conn, conn.cursor() as cur:
        # buang hasil FASE A sebelumnya dengan tag sama supaya angka tidak menumpuk
        # (run FASE B tidak disentuh: filternya dari notes)
        cur.execute("""
            DELETE FROM eval.eval_runs WHERE notes LIKE 'FASE A%%' AND eval_config_id IN
                (SELECT id FROM eval.eval_configs WHERE name LIKE %s)
        """, (f"{args.tag}-k%",))
        print(f"hasil lama tag '{args.tag}' dibersihkan ({cur.rowcount} run)\n")
        for t in daftar_t:
            for k in daftar_k:
                nama = f"{args.tag}-k{k}-t{int(t * 100):02d}"
                cur.execute("""
                    INSERT INTO eval.eval_configs
                        (name, top_k, threshold, temperature, embed_model, llm_model, prompt_version, notes)
                    VALUES (%s, %s, %s, %s, %s, '-', 'v1', 'FASE A: sapuan retrieval tanpa LLM')
                    ON CONFLICT (name) DO UPDATE SET top_k = EXCLUDED.top_k,
                        threshold = EXCLUDED.threshold, notes = EXCLUDED.notes
                    RETURNING id
                """, (nama, k, t, args.suhu, rag.EMBED_MODEL))
                cfg_id = cur.fetchone()[0]

                cur.execute("""
                    INSERT INTO eval.eval_runs (eval_config_id, git_commit, notes)
                    VALUES (%s, %s, %s) RETURNING id
                """, (cfg_id, git_short_hash(), "FASE A: retrieval saja, tanpa LLM"))
                run_id = cur.fetchone()[0]

                recall, recall_gold, presisi, token, latensi = [], [], [], [], []
                tolak = tolak_benar = 0
                skor_negatif = []
                for s in soal:
                    hits = pick_chunks(kandidat[s["kode"]], t, k)
                    ref = s["acuan"]
                    jumlah = len(hits)
                    tok = sum(len(h["text"]) for h in hits) // 4
                    ms = s["latensi_sql_ms"]

                    cur.execute("""
                        INSERT INTO eval.eval_answers
                            (eval_run_id, eval_question_id, answer, contexts, sources, latency_ms)
                        VALUES (%s, %s, NULL, '[]'::jsonb, %s::jsonb, %s) RETURNING id
                    """, (run_id, s["id"],
                          json.dumps([{"file": h["source"], "score": round(h["score"], 4)}
                                      for h in hits]), ms))
                    jawab_id = cur.fetchone()[0]

                    for peringkat, h in enumerate(hits, 1):
                        cur.execute("""
                            INSERT INTO eval.eval_retrieval_hits
                                (eval_answer_id, chunk_id, rank, score) VALUES (%s, %s, %s, %s)
                        """, (jawab_id, h["chunk_id"], peringkat, round(h["score"], 4)))

                    nilai = {"n_potongan": jumlah, "token_konteks": tok}
                    if ref:
                        id_hits = {h["chunk_id"] for h in hits}
                        kena = len(ref & id_hits)
                        nilai["recall_acuan"] = 1.0 if kena else 0.0
                        nilai["recall_gold"] = 1.0 if (s["gold"] & id_hits) else 0.0
                        nilai["presisi_acuan"] = kena / jumlah if jumlah else 0.0
                        recall.append(nilai["recall_acuan"])
                        recall_gold.append(nilai["recall_gold"])
                        presisi.append(nilai["presisi_acuan"])
                        token.append(tok)
                    else:  # kategori negatif: benar = sistem tidak menemukan apa pun
                        tolak += 1
                        tolak_benar += 1 if jumlah == 0 else 0
                        nilai["tolak_benar"] = 1.0 if jumlah == 0 else 0.0
                        skor_negatif.append(hits[0]["score"] if hits else 0.0)
                        nilai["skor_teratas_negatif"] = round(hits[0]["score"], 4) if hits else 0.0
                    latensi.append(ms)

                    for metrik, v in nilai.items():
                        cur.execute("""
                            INSERT INTO eval.eval_scores (eval_answer_id, metric, value)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (eval_answer_id, metric) DO UPDATE SET value = EXCLUDED.value
                        """, (jawab_id, metrik, float(v)))

                cur.execute("UPDATE eval.eval_runs SET finished_at = now() WHERE id = %s", (run_id,))
                ringkas.append({
                    "nama": nama, "k": k, "t": t, "run": run_id,
                    "recall": statistics.mean(recall) if recall else 0.0,
                    "gold": statistics.mean(recall_gold) if recall_gold else 0.0,
                    "presisi": statistics.mean(presisi) if presisi else 0.0,
                    "token": statistics.mean(token) if token else 0.0,
                    "latensi": statistics.mean(latensi),
                    "skor_neg": statistics.mean(skor_negatif) if skor_negatif else 0.0,
                    "tolak": f"{tolak_benar}/{tolak}" if tolak else "-",
                })

    print(f"{'konfigurasi':14} {'k':>3} {'thr':>5} {'recall':>7} {'r_gold':>7} {'presisi':>8} "
          f"{'token':>6} {'top_neg':>8} {'tolak':>7}")
    for r in sorted(ringkas, key=lambda r: (-r["gold"], r["token"])):
        print(f"{r['nama']:14} {r['k']:>3} {r['t']:>5.2f} {r['recall']:>7.3f} {r['gold']:>7.3f} "
              f"{r['presisi']:>8.3f} {r['token']:>6.0f} {r['skor_neg']:>8.3f} {r['tolak']:>7}")
    print("\n'r_gold' = recall terhadap potongan acuan terbaik saja (lebih ketat dari 'recall')."
          "\n'top_neg' = rata-rata skor teratas pada 10 pertanyaan negatif (makin rendah makin baik).")


if __name__ == "__main__":
    main()
