import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(DIR.parent / ".env", override=True)

import db  # noqa: E402

CACHE = DIR / ".cache" / "embedding.json"
METRIK_DASAR = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]


# utilitas

def question_vector(teks: str) -> list[float]:
    import rag
    CACHE.parent.mkdir(exist_ok=True)
    isi = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    kunci = hashlib.md5(teks.encode("utf-8")).hexdigest()
    if kunci in isi:
        return isi[kunci]
    v = rag.get_embedding(teks)
    isi[kunci] = v
    CACHE.write_text(json.dumps(isi), encoding="utf-8")
    return v


def pick_chunks(kandidat, threshold, top_k):
    seen, hasil = set(), []
    for item in kandidat:
        if item["score"] < threshold:
            continue
        k = item["text"].strip()
        if k in seen:
            continue
        seen.add(k)
        hasil.append(item)
        if len(hasil) == top_k:
            break
    return hasil


def load_config(cur, nama: str) -> dict:
    cur.execute("SELECT id, name, top_k, threshold, temperature FROM eval.eval_configs WHERE name = %s", (nama,))
    r = cur.fetchone()
    if not r:
        sys.exit(f"konfigurasi '{nama}' tidak ada — jalankan eval/fase_a_retrieval.py dulu")
    return {"id": r[0], "nama": r[1], "k": r[2], "t": r[3], "suhu": r[4]}


# jawab

def answer_all(nama: str, limit: int, jenis: set[str], ulang: bool = False) -> None:
    import rag
    with db.connect_eval() as conn, conn.cursor() as cur:
        cfg = load_config(cur, nama)
        cur.execute("SELECT id, code, question, category FROM eval.eval_questions ORDER BY code")
        soal = [s for s in cur.fetchall() if s[3].split("/")[0] in jenis] if jenis else cur.fetchall()
        if limit:
            soal = soal[:limit]

        try:
            from importlib.metadata import version as _v
            versi_ragas = _v("ragas")
        except Exception:
            versi_ragas = ""
        penilai = os.getenv("EVAL_JUDGE_MODEL", "") or rag.LLM_MODEL or rag.GEMINI_LLM_MODEL

        # TITIK PERIKSA: kalau run terakhir sudah punya jawaban, lanjutkan run yang sama dan
        # lewati soal yang sudah dijawab -> proses yang mati di tengah tidak mengulang dari awal.
        cur.execute("""SELECT r.id, count(a.id) FILTER (WHERE a.answer IS NOT NULL)
                       FROM eval.eval_runs r
                       LEFT JOIN eval.eval_answers a ON a.eval_run_id = r.id
                       WHERE r.eval_config_id = %s GROUP BY r.id ORDER BY r.id DESC LIMIT 1""",
                    (cfg["id"],))
        ada = cur.fetchone()
        if ada and ada[1] and not ulang:
            run_id = ada[0]
            cur.execute("""SELECT eval_question_id FROM eval.eval_answers
                           WHERE eval_run_id = %s AND answer IS NOT NULL""", (run_id,))
            sudah = {baris[0] for baris in cur.fetchall()}
            soal = [s for s in soal if s[0] not in sudah]
            print(f"run #{run_id} dilanjutkan: {len(sudah)} soal sudah dijawab, {len(soal)} sisa")
        else:
            cur.execute("""
                INSERT INTO eval.eval_runs (eval_config_id, git_commit, ragas_version, judge_model, notes)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (cfg["id"], "", versi_ragas, penilai,
                  f"FASE B: jawaban (k={cfg['k']}, threshold={cfg['t']}, suhu={cfg['suhu']})"))
            run_id = cur.fetchone()[0]
            print(f"run #{run_id} untuk {cfg['nama']}: {len(soal)} pertanyaan")
        if not soal:
            print("semua soal sudah dijawab — tidak ada yang dikerjakan")
            return

        for s in soal:
            t0 = time.perf_counter()
            hits = pick_chunks(db.fetch_candidates(question_vector(s[2]), -1.0), cfg["t"], cfg["k"])
            if hits:
                jawaban, pesan = None, ""
                for percobaan in range(3):
                    try:
                        jawaban = rag.generate(rag.build_prompt(s[2], hits), temperature=cfg["suhu"])
                        break
                    except Exception as e:
                        pesan = str(e)[:120]
                        print(f"    [ulang {percobaan + 1}/3] {s[1]}: {pesan}")
                        time.sleep(10 * (percobaan + 1))
                if jawaban is None:
                    # dicatat apa adanya, pertanyaan tetap tersimpan supaya bisa dinilai/diulang
                    jawaban = f"GAGAL_DIJAWAB: {pesan}"
            else:
                jawaban = "Maaf, informasi yang Anda cari tidak ditemukan."
            ms = int((time.perf_counter() - t0) * 1000)

            cur.execute("""
                INSERT INTO eval.eval_answers
                    (eval_run_id, eval_question_id, answer, contexts, sources, latency_ms)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s) RETURNING id
            """, (run_id, s[0], jawaban,
                  json.dumps([h["text"] for h in hits]),
                  json.dumps([{"file": h["source"], "score": round(h["score"], 4)} for h in hits]), ms))
            jawab_id = cur.fetchone()[0]
            for peringkat, h in enumerate(hits, 1):
                cur.execute("""
                    INSERT INTO eval.eval_retrieval_hits (eval_answer_id, chunk_id, rank, score)
                    VALUES (%s, %s, %s, %s)
                """, (jawab_id, h["chunk_id"], peringkat, round(h["score"], 4)))

            cur.execute("""
                INSERT INTO eval.eval_scores (eval_answer_id, metric, value)
                VALUES (%s, 'n_potongan', %s), (%s, 'token_konteks', %s)
                ON CONFLICT (eval_answer_id, metric) DO UPDATE SET value = EXCLUDED.value
            """, (jawab_id, len(hits), jawab_id, sum(len(h["text"]) for h in hits) // 4))

            conn.commit()  # commit per soal: progres terlihat & kerja tidak hilang kalau proses mati
            print(f"  {s[1]:22} {len(hits):>2} potongan {ms:>6} ms | {jawaban.strip()[:70]}", flush=True)

        cur.execute("UPDATE eval.eval_runs SET finished_at = now() WHERE id = %s", (run_id,))
    print(f"\njawaban tersimpan di eval.eval_answers (run #{run_id})")


# skor

def build_judge(judge_base: str = "", judge_model: str = ""):
    from fastembed import TextEmbedding
    from langchain_core.embeddings import Embeddings
    from langchain_openai import ChatOpenAI
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    import rag

    class EmbedLokal(Embeddings):
        """Pembungkus tipis fastembed -> antarmuka Embeddings langchain.

        Dipakai langsung, tanpa langchain_community.FastEmbedEmbeddings: kelas itu menyimpan
        objek TextEmbedding pada field `model`, dan ragas 0.3 gagal memvalidasinya
        ("Input should be a valid string").
        """

        def __init__(self, nama: str):
            self.nama = nama
            self._mesin = TextEmbedding(model_name=nama)

        def embed_documents(self, teks: list[str]) -> list[list[float]]:
            return [v.tolist() for v in self._mesin.embed(teks)]

        def embed_query(self, teks: str) -> list[float]:
            return next(iter(self._mesin.query_embed(teks))).tolist()

    if not judge_base and not os.getenv("EVAL_JUDGE_BASE_URL") and not rag.LLM_BASE_URL:
        sys.exit(
            "Penilai RAGAS dimatikan: semua base URL kosong. Untuk menilai, isi "
            "EVAL_JUDGE_BASE_URL + EVAL_JUDGE_MODEL (gateway OpenAI-compatible) di .env. "
            "Jalur judge Gemini sengaja dihapus."
        )
    dasar = judge_base or os.getenv("EVAL_JUDGE_BASE_URL", "") or rag.LLM_BASE_URL
    kunci = os.getenv("EVAL_JUDGE_API_KEY", "") or rag.LLM_API_KEY
    if not kunci:
        sys.exit("EVAL_JUDGE_API_KEY (atribut penilai) kosong — isi di .env")
    llm = ChatOpenAI(base_url=dasar, api_key=kunci,
                     model=judge_model or os.getenv("EVAL_JUDGE_MODEL", "") or rag.LLM_MODEL or rag.GEMINI_LLM_MODEL,
                     temperature=0.0, timeout=180, max_retries=4)
    # embedding lokal (offline, tanpa kuota API) khusus metrik RAGAS
    emb = EmbedLokal(os.getenv("EVAL_EMBED_MODEL",
                               "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))
    return LangchainLLMWrapper(llm), LangchainEmbeddingsWrapper(emb)


def _write_scores(sepotong: list[dict], tabel, judge: str = "") -> None:
    """Simpan nilai satu potongan; nilai gagal (NaN) ditulis NULL dan tidak menimpa nilai bagus.
    `judge` = model penilai yang benar-benar dipakai, disimpan per baris supaya asal-usul nilai
    bisa dibuktikan dari data (tabel run saja tidak cukup: satu run bisa dinilai berganti penilai)."""
    with db.connect_eval() as conn, conn.cursor() as cur:
        for d, (_, r) in zip(sepotong, tabel.iterrows()):
            for m in tabel.columns:
                if m in ("user_input", "response", "retrieved_contexts", "reference"):
                    continue
                nilai = r[m]
                if nilai is None or (isinstance(nilai, float) and nilai != nilai):
                    bersih = None
                else:
                    bersih = float(nilai)
                cur.execute("""
                    INSERT INTO eval.eval_scores (eval_answer_id, metric, value, judge_model)
                    VALUES (%s, %s, %s, %s) ON CONFLICT (eval_answer_id, metric)
                    DO UPDATE SET value = COALESCE(EXCLUDED.value, eval.eval_scores.value),
                                  judge_model = COALESCE(EXCLUDED.judge_model, eval.eval_scores.judge_model)
                """, (d["id"], m, bersih, judge or None))


def score_all(nama: str, limit: int, lengkap: bool, judge_base: str = "", judge_model: str = "",
         bagian: str = "", ulang: bool = False, bersih: bool = False) -> None:
    from ragas import EvaluationDataset, evaluate
    from ragas import SingleTurnSample
    import ragas.metrics as R

    with db.connect_eval() as conn, conn.cursor() as cur:
        cfg = load_config(cur, nama)
        if bersih:
            # ganti penilai = nilai lama dibuang, bukan ditimpa COALESCE (kalau ditimpa, nilai
            # penilai lama tetap tinggal ketika yang baru gagal -> tabel mencampur dua penilai)
            cur.execute("""DELETE FROM eval.eval_scores s USING eval.eval_answers a
                           WHERE s.eval_answer_id = a.id AND a.eval_run_id =
                                 (SELECT id FROM eval.eval_runs WHERE eval_config_id = %s
                                  ORDER BY id DESC LIMIT 1)""", (cfg["id"],))
            print(f"nilai lama dibersihkan: {cur.rowcount} baris")
        # context_precision memanggil LLM sekali PER POTONGAN (k=8 -> 8 panggilan), jadi dia yang
        # membuat penilaian mahal. Sapuan pembanding cukup 3 metrik (4 panggilan/soal); konfigurasi
        # pemenang dinilai ulang dengan 4 metrik.
        pilih_metrik = [m.strip() for m in os.getenv("EVAL_METRIK", "").split(",") if m.strip()]
        metrik = pilih_metrik or (METRIK_DASAR + (["answer_correctness"] if lengkap else []))
        # terisi = berapa metrik yang sudah ada; dipakai supaya penilaian bisa dijalankan
        # bertahap (batas per menit penilai) tanpa mengulang yang sudah selesai
        cur.execute("""
            SELECT a.id, q.code, q.question, q.ground_truth, a.answer, a.contexts,
                   (SELECT count(*) FROM eval.eval_scores s
                     WHERE s.eval_answer_id = a.id AND s.metric = ANY(%s)
                       AND s.value IS NOT NULL) AS terisi
            FROM eval.eval_answers a
            JOIN eval.eval_questions q ON q.id = a.eval_question_id
            JOIN eval.eval_runs r ON r.id = a.eval_run_id
            WHERE r.eval_config_id = %s AND a.answer IS NOT NULL
            ORDER BY r.id DESC, q.code
        """, (metrik, cfg["id"]))
        semua = cur.fetchall()
        belum = semua if ulang else [b for b in semua if b[6] < len(metrik)]
        if bagian:  # pecah antar proses paralel: '2/3' ambil indeks 1,4,7,...
            i, n = (int(x) for x in bagian.split("/"))
            belum = belum[i - 1::n]
        if limit:
            belum = belum[:limit]
        baris = belum
        print(f"{len(semua)} jawaban tersimpan, {len(semua) - len(belum)} sudah lengkap, "
              f"{len(belum)} akan dinilai")
        if not baris:
            print("tidak ada yang perlu dinilai — semua jawaban sudah punya seluruh metrik")
            return

        daftar = [{"id": b[0], "code": b[1], "q": b[2], "gt": b[3], "answer": b[4],
                   "ctx": b[5] if isinstance(b[5], list) else json.loads(b[5])} for b in baris]

    tanpa_konteks = [d for d in daftar if not d["ctx"]]
    daftar = [d for d in daftar if d["gt"]]
    if tanpa_konteks:
        print(f"catatan: {len(tanpa_konteks)} jawaban tanpa konteks (ditolak threshold) tetap dinilai")
    daftar = [{**d, "ctx": d["ctx"] or [""]} for d in daftar]
    print(f"menilai {len(daftar)} jawaban dengan RAGAS (penilai: {nama})")

    llm, emb = build_judge(judge_base, judge_model)
    # Baris run dibuat saat --answer memakai penilai dari .env, padahal penilaian bisa jalan
    # dengan penilai lain lewat --judge-model. Catat yang BENAR dipakai, kalau tidak matriks
    # pembanding salah label (pernah kejadian: k7 berlabel gemini padahal dinilai gateway).
    label = judge_model or os.getenv("EVAL_JUDGE_MODEL", "") or rag.GEMINI_LLM_MODEL
    with db.connect_eval() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE eval.eval_runs SET judge_model = %s
                       WHERE id = (SELECT id FROM eval.eval_runs WHERE eval_config_id = %s
                                   ORDER BY id DESC LIMIT 1)""", (label, cfg["id"]))
    metrik = [getattr(R, m) for m in metrik]
    # endpoint OpenAI-compatible (Gemini) menolak n>1: "Multiple candidates is not enabled
    # for this model" -> answer_relevancy minta 1 pertanyaan balik, bukan 3 (strictness=3)
    metrik_map = {m.name: m for m in metrik}
    if "answer_relevancy" in metrik_map:
        metrik_map["answer_relevancy"].strictness = int(os.getenv("EVAL_STRICTNESS", "1"))
    # Penilai gratis membatasi laju: satu panggilan evaluate dengan puluhan job sekaligus
    # membuat SEMUA job timeout dan seluruh nilai tertulis NULL. Jadi dinilai per potongan
    # kecil, concurrency dijepit, hasilnya ditulis tiap potongan supaya progres kelihatan.
    from ragas import RunConfig
    ukuran = int(os.getenv("EVAL_BATCH", "4"))
    rc = RunConfig(max_workers=int(os.getenv("EVAL_WORKERS", "8")),
                   timeout=int(os.getenv("EVAL_JUDGE_TIMEOUT", "300")))
    print(f"potongan {ukuran} soal, {rc.max_workers} pekerja, timeout {rc.timeout} s "
          f"({len(daftar)} soal -> {len(range(0, len(daftar), ukuran))} potongan), "
          f"metrik: {', '.join(m.name for m in metrik)}", flush=True)

    tabel_potongan = []
    for mulai in range(0, len(daftar), ukuran):
        sepotong = daftar[mulai:mulai + ukuran]
        dataset = EvaluationDataset(samples=[
            SingleTurnSample(user_input=d["q"], response=d["answer"],
                             retrieved_contexts=d["ctx"], reference=d["gt"]) for d in sepotong
        ])
        print(f"[{time.strftime('%H:%M:%S')}] potongan {mulai // ukuran + 1}: soal "
              f"{sepotong[0]['code']}..{sepotong[-1]['code']}", flush=True)
        hasil = evaluate(dataset=dataset, metrics=metrik, llm=llm, embeddings=emb,
                         raise_exceptions=False, show_progress=False, run_config=rc)
        tabel = hasil.to_pandas()
        tabel_potongan.append((sepotong, tabel))
        _write_scores(sepotong, tabel, label)
        terisi = sum(1 for _, r in tabel.iterrows() for m in tabel.columns
                     if m not in ("user_input", "response", "retrieved_contexts", "reference")
                     and r[m] == r[m] and r[m] is not None)
        print(f"   -> {terisi} nilai terisi dari {len(sepotong) * len(metrik)} slot "
              f"({mulai + len(sepotong)}/{len(daftar)} soal selesai)", flush=True)
        time.sleep(3)

    kolom = [m.name for m in metrik]
    print(f"\n{'pertanyaan':24}" + "".join(f"{m:>20}" for m in kolom))
    for sepotong, tabel in tabel_potongan:
        for d, (_, r) in zip(sepotong, tabel.iterrows()):
            print(f"{d['code']:24}" + "".join(
                f"{(r[m] if r[m] == r[m] and r[m] is not None else float('nan')):>20.3f}"
                for m in kolom))
    for m in kolom:
        nilai = [r[m] for _, t in tabel_potongan for (_, r) in t.iterrows()
                 if r[m] == r[m] and r[m] is not None] if tabel_potongan else []
        print(f"rata-rata {m:24} = {sum(nilai) / len(nilai):.3f}  (n={len(nilai)})"
              if nilai else f"rata-rata {m:24} = tidak ada nilai")

    print("nilai tersimpan; lihat progress: .venv/Scripts/python.exe eval/progress.py")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="nama konfigurasi di eval.eval_configs, mis. A-k8-t60")
    ap.add_argument("--answer", action="store_true", help="susun jawaban untuk konfigurasi ini")
    ap.add_argument("--score", "--skor", dest="score", action="store_true",
                    help="nilai jawaban dengan RAGAS (butuh penilai di .env)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jenis", default="", help="batasi kategori, mis. 'parafrase,sintesis,negatif'")
    ap.add_argument("--bagian", default="", help="bagi pekerjaan antar proses paralel, mis. '2/3'")
    ap.add_argument("--ulang", action="store_true",
                    help="abaikan titik periksa: jawab/nilai ulang semuanya dari nol")
    ap.add_argument("--judge-base", default="", help="kosongkan = ikut EVAL_JUDGE_BASE_URL/.env")
    ap.add_argument("--judge-model", default="", help="nama model penilai, mis. tokenharbor/deepseek-v4-flash:free")
    ap.add_argument("--lengkap", action="store_true", help="tambahkan metrik answer_correctness")
    ap.add_argument("--bersih", action="store_true",
                    help="hapus dulu semua nilai run terakhir (dipakai saat ganti penilai)")
    args = ap.parse_args()

    jenis = {j.strip() for j in args.jenis.split(",") if j.strip()}
    if args.answer:
        answer_all(args.config, args.limit, jenis, args.ulang)
    if args.score:
        score_all(args.config, args.limit, args.lengkap, args.judge_base, args.judge_model,
                  args.bagian, args.ulang, args.bersih)
    if not args.answer and not args.score:
        sys.exit("pilih --answer dan/atau --score")


if __name__ == "__main__":
    main()
