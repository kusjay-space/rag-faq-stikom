# RAG FAQ Kurikulum — ITB STIKOM Bali

Chatbot akademik berbasis **Retrieval-Augmented Generation (RAG)**. Pertanyaan dijawab **hanya** dari
dokumen resmi kurikulum dan profil institusi di `data/docs-kurikulum/`, lengkap dengan daftar sumber
dan skor kemiripan. Tidak ada model yang di-fine-tune — seluruh pengetahuan berasal dari berkas teks
yang bisa diedit siapa pun tanpa menyentuh kode.

---

## Fitur

| Fitur | Keterangan |
| :-- | :-- |
| Pencarian semantik | Pakai embedding (768 dimensi), cocok berdasarkan **makna**, bukan kecocokan kata |
| Jawaban bergrounding | Model dilarang menjawab di luar konteks dokumen |
| Transparansi sumber | Setiap jawaban menyertakan berkas sumber + skor kemiripan |
| Penyimpanan vektor di PostgreSQL | pgvector + index HNSW; data ikut ter-backup bersama tabel lain |
| Telemetri pencarian | Pertanyaan, jumlah hasil, skor teratas, dan latensi dicatat di `rag.retrieval_logs` |
| UI siap pakai | Satu halaman HTML, tanpa build step |
| Evaluasi terukur | FASE A: 15 konfigurasi x 50 soal inti (gratis, tanpa LLM). FASE B (RAGAS) 50 soal x 4 metrik — penilai kini dimatikan |

---

## Arsitektur

```
TAHAP 1 — INGEST (manual, saat dokumen berubah)          python ingest.py
  data/docs-kurikulum/*.md
        │  potong per paragraf; buang <30 karakter, heading gantung, baris "Sumber data:"
        ▼
  Gemini Embedding API  ──►  rag.documents + rag.chunks (vector(768))
                             progres dicatat di rag.pipeline_runs

TAHAP 2 — MENJAWAB (per pertanyaan)                      python app.py
  pertanyaan user
        │  embed pertanyaan
        ▼
  SELECT ... ORDER BY embedding <=> vektor  →  buang skor < threshold
        │  buang teks kembar, ambil TOP_K teratas
        ▼
  prompt (TOP_K potongan + nomor + nama berkas)  ──►  model bahasa (temperature 0.0,
                                                        gateway DeepSeek atau Gemini langsung)
        │
        ▼
  { "answer": ..., "sources": [{file, score}, ...] }  ──►  Flask /api/tanya

EVALUASI (terpisah, schema `eval`)                       lihat bagian Evaluasi di bawah
```

---

## Struktur Berkas

```
rag-faq-stikom/
├── app.py                      # server Flask + endpoint POST /api/tanya
├── rag.py                      # get_embedding / retrieve / build_prompt / generate / ask_question
├── db.py                       # satu-satunya modul yang tahu nama schema & tabel PostgreSQL
├── ingest.py                   # potong dokumen -> embedding -> simpan ke PostgreSQL
├── index.html                  # antarmuka chat (HTML + CSS + JS, tanpa build step)
├── eval/                       # bank soal + FASE A (gratis) + FASE B (RAGAS) + pelaporan
#   eval/laporan.py            ringkasan hasil evaluasi dari DB
#   eval/laporan_operasional.py ringkasan pemakaian harian dari rag.retrieval_logs
├── docs/diagram/               # *.png: arsitektur sistem, alur FASE A, alur FASE B (+ sumber .excalidraw)
├── requirements.txt            # aplikasi + evaluasi (satu berkas, Python 3.12)
├── sql/schema.sql              # skema lengkap dari nol (runtime + evaluasi + ekstensi + hak akses)
├── .env.example                # contoh konfigurasi (aman di-commit)
├── .env                        # konfigurasi asli + API key (TIDAK di-commit)
├── README.md                   # berkas ini
└── data/docs-kurikulum/        # SUMBER KEBENARAN — 9 dokumen .md (kurikulum, profil, dosen, karyawan)
```

`data/docs-kurikulum/` adalah sumber kebenaran. Memperbaiki jawaban = memperbaiki dokumen, lalu
jalankan `python ingest.py`.

---

## Cara Menjalankan

### 1. Siapkan environment

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Siapkan PostgreSQL + pgvector

Laragon (Windows) sudah membawa PostgreSQL + pgvector — cukup nyalakan service-nya.

```bash
# sekali saja: buat database dan role aplikasi
psql -U postgres -h 127.0.0.1 -c "CREATE DATABASE rag_kurikulum;"
psql -U postgres -h 127.0.0.1 -c "CREATE ROLE rag_app LOGIN PASSWORD 'ganti-password-ini';"

# pasang skema lengkap (butuh superuser untuk CREATE EXTENSION vector)
psql -U postgres -h 127.0.0.1 -d rag_kurikulum -f sql/schema.sql
```

### 3. Konfigurasi

```bash
copy .env.example .env          # Windows
```

Isi `GEMINI_API_KEY` (ambil di <https://aistudio.google.com/apikey>) dan `DATABASE_URL`.

### 4. Isi data

```bash
python ingest.py            # hanya berkas baru/berubah yang di-embed
python ingest.py --force    # paksa embed ulang semuanya (misal setelah ganti model/chunking)
```

Jalankan berulang kali sebisa mungkin: berkas yang isinya tidak berubah dilewati tanpa panggilan API.
Rinciannya di [Sinkronisasi data](#sinkronisasi-data).

### 5. Jalankan

```bash
python app.py
```

Buka <http://127.0.0.1:5000>. Endpoint API: `POST /api/tanya` body `{"question": "..."}`.

---

## Basis Data

```
DATABASE rag_kurikulum
├── schema rag    (runtime)   documents, chunks, pipeline_runs, retrieval_logs
├── schema eval   (offline)   eval_questions, _question_chunks, _configs, _runs,
│                             _answers, _scores, _retrieval_hits, v_config_comparison
├── schema ext    (pgvector)  118 fungsi/operator vector_*
└── schema public  kosong
```

Kenapa begitu:

- **`rag` vs `eval`** — objek untuk melayani pengguna dipisah dari objek pengukuran kualitas.
  `DROP SCHEMA eval CASCADE;` tidak merusak aplikasi, dan backup tabel produksi tidak membengkak oleh
  ratusan baris jawaban evaluasi.
- **`ext`** — `CREATE EXTENSION vector` tanpa `SCHEMA` menaruh 118 fungsi `vector_*` di `public` dan
  tercampur dengan tabel kita saat dibuka di GUI.
- **`public` kosong** — sengaja; setiap objek punya rumah yang jelas.
- `content_md5` **bukan** `UNIQUE`: teks boilerplate berulang antar berkas (666 baris, 639 teks unik).
  Dedupe dikerjakan saat query di `rag.retrieve()`.

Hak akses: `rag_app` (runtime) dan `rag_eval` (evaluasi); keduanya bukan superuser.
`search_path = rag, ext, public` dipaksa di dua tempat — di role server **dan** saat konek (`db.py`) —
supaya aplikasi tetap benar di database baru tanpa bergantung setelan server.

Sengaja **tidak** dipakai: partisi tabel, soft delete, kolom jumlah token, trigger `updated_at`,
materialized view. Skalanya masih ratusan baris; semuanya menambah kerumitan tanpa manfaat.

### Catatan implementasi

- **Urutan hasil dibuat deterministik.** Skor dibulatkan 3 desimal (angka yang ditampilkan ke
  pengguna), seri dipecah nama berkas + isi dengan `COLLATE "C"` (urutan byte = urutan `str` Python).
  Sebabnya pgvector menghitung jarak dalam float4 (selisih terukur ≤ 6e-7 vs NumPy float64) — tanpa
  ini urutan potongan berskor hampir sama bisa berubah antar eksekusi.
- **`seq` = nomor paragraf di dalam berkas**, dan `(document_id, seq)` unik. Itulah yang membuat
  `ingest.py` boleh dijalankan ulang tanpa menggandakan data.
- **Dedupe teks dikerjakan saat query** (`rag.retrieve()`), bukan lewat `UNIQUE` di DB — supaya satu
  teks hanya muncul sekali di hasil walau ada di beberapa berkas.
- **`latency_ms` di `retrieval_logs`** mencakup panggilan embedding Gemini + query database, bukan
  hanya SQL.
- **Aturan pemotongan** (`ingest.py`) sengaja belum diubah dari versi awal: potong per paragraf,
  buang <30 karakter, buang heading gantung, buang baris `Sumber data:`. Angka evaluasi sebelum/sesudah
  perbaikan chunking nanti hanya sahih kalau aturan lama ini dikembalikan lagi.
- Tabel `eval.*` **kosong sampai evaluasi dijalankan** (lihat bagian Evaluasi).

### Sinkronisasi data

`ingest.py` membandingkan `sha256` tiap berkas `.md` dengan yang tercatat di `rag.documents`, lalu hanya
mengerjakan yang perlu. Database selalu jadi cermin folder dokumen — tidak ada langkah "bersihkan DB"
manual (dan tidak ada `TRUNCATE` di mana pun).

| Kejadian di `data/docs-kurikulum/` | Yang dikerjakan `ingest.py` | Panggilan API |
| :-- | :-- | :-- |
| Tidak ada yang berubah | Lewat. Tidak ada baris `pipeline_runs` baru. | **0** |
| Berkas baru | Embed berkas itu, `INSERT` dokumen + potongan | hanya berkas itu |
| Isi berkas berubah | `DELETE` potongan berkas itu, embed ulang dari nol | hanya berkas itu |
| Berkas dipendekkan | Sama seperti di atas → **potongan lama tidak tersisa** | hanya berkas itu |
| Berkas dihapus dari disk | Dokumen + potongannya ikut `DELETE` | 0 |
| Ganti `EMBED_MODEL`/`EMBED_DIM`/aturan chunking | `--force` untuk embed ulang semua | seluruh korpus |

Potongan berkas yang berubah dihapus dulu sebelum ditulis ulang (bukan ditimpa per nomor), jadi sisa
potongan dari versi dokumen yang lebih panjang tidak mungkin tertinggal. `sha256` di `rag.documents`
ditulis paling akhir sebagai penanda berkas sudah utuh, jadi ingest yang mati di tengah jalan akan
mengulang berkas itu pada eksekusi berikutnya — bukan menganggapnya beres. Setiap eksekusi yang
benar-benar mengerjakan sesuatu menulis jejaknya di `rag.pipeline_runs` (`status` = `running` /
`completed` / `failed`, `chunk_count`, `chunks_done`, `notes` kalau gagal).

---

## Konfigurasi (`.env`)

| Variabel | Default | Fungsi |
| :-- | :-- | :-- |
| `GEMINI_API_KEY` | — | Kunci API Google Gemini (embedding + jalur LLM Gemini langsung). **Jangan pernah di-commit.** |
| `EMBED_MODEL` | `gemini-embedding-001` | Model pengubah teks jadi vektor (selalu Gemini) |
| `GEMINI_LLM_MODEL` | `gemini-3.1-flash-lite` | Model penyusun jawaban pada jalur Gemini langsung (dipakai **hanya bila** `LLM_BASE_URL` kosong) |
| `LLM_BASE_URL` | — | Endpoint OpenAI-compatible untuk LLM, mis. `http://homelab-jay:20128/v1`. **Kosong = langsung ke Gemini** |
| `LLM_API_KEY` | — | Kunci gateway (tidak dipakai kalau `LLM_BASE_URL` kosong) |
| `LLM_MODEL` | — | Id model di gateway, mis. `tokenharbor/deepseek-v4-flash:free`; **wajib terisi** kalau `LLM_BASE_URL` diisi |
| `EMBED_DIM` | `768` | Panjang vektor; **wajib sama** saat ingest dan query |
| `DOCS_DIR` | `data/docs-kurikulum` | Folder dokumen sumber |
| `SIMILARITY_THRESHOLD` | `0.60` | Ambang skor; di bawah ini potongan dibuang |
| `TOP_K` | `8` | Jumlah potongan konteks yang dikirim ke LLM |

**Aturan penamaan (jangan dicampur).** Satu variabel untuk satu jalur:
- `EMBED_MODEL` + `EMBED_DIM` — embedding, **selalu** lewat Gemini; dipakai `ingest.py` dan `rag.py`.
- `GEMINI_LLM_MODEL` — model Gemini langsung; hanya dipakai bila `LLM_BASE_URL` kosong.
- `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` — jalur gateway OpenAI-compatible.

Tidak ada lagi `MODEL_LLM`/`MODEL_EMBED`: dulu dua nama itu mudah tertukar dengan `LLM_MODEL`.

| `DATABASE_URL` | — | Koneksi PostgreSQL untuk aplikasi (`rag_app`); password hanya di `.env` |
| `EVAL_DATABASE_URL` | — | Koneksi role `rag_eval` untuk skrip `eval/`: tulis schema `eval`, baca `rag.*` |
| `EVAL_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Embedding **lokal** (fastembed) untuk metrik RAGAS — nol kuota API |
| `INGEST_JEDA_DETIK` | `65` | Jeda antar batch 80 teks saat ingest (hindari `429`) |

> Semua modul memuat `.env` dengan `load_dotenv(..., override=True)`: **nilai di `.env` selalu
> menang** atas variabel lingkungan shell. Sebelumnya `override` default (`False`) membuat variabel
> shell lama (mis. `EVAL_JUDGE_BASE_URL` sisa sesi sebelumnya) diam-diam menimpa `.env` — penilaian
> jadi berjalan lewat penilai yang salah tanpa peringatan.

Mengubah `EMBED_DIM` atau `EMBED_MODEL` membuat vektor lama tidak kompatibel — jalankan `ingest.py`
dari nol (untuk uji cepat tanpa jeda: `INGEST_JEDA_DETIK=0 python ingest.py`).

### Model LLM: langsung ke Gemini atau lewat gateway

`rag.generate()` punya dua jalur, dipilih otomatis dari `LLM_BASE_URL`:

| `LLM_BASE_URL` | Yang dipakai | Model |
| :-- | :-- | :-- |
| kosong | SDK `google-genai` | `GEMINI_LLM_MODEL` |
| terisi (mis. 9router) | HTTP `POST {base}/chat/completions` pakai pustaka bawaan Python | `LLM_MODEL` |

Poin penting:

- **Embedding tidak ikut berpindah.** Untuk gateway OpenAI-compatible, vektor tetap dari
  `EMBED_MODEL` (Gemini) — jadi DB tidak perlu di-ingest ulang.
- Sebagian gateway menempelkan penanda SSE `data: [DONE]` di ekor respons non-stream; `rag.py`
  membaca objek JSON pertama (`raw_decode`) supaya tidak gagal parse. Ini pernah bikin error
  `JSONDecodeError: Extra data`.
- Mengganti model LLM mengubah isi jawaban. Kalau tabel evaluasi dibandingkan, **satu tabel = satu
  model**; jangan campur jawaban dua model tanpa menandainya.

---

## Evaluasi (`eval/`)

Cara kerja FASE A vs RAGAS, diagram alur (`docs/diagram/*.png`), tabel hasil, keterbatasan, dan
kesimpulan ada di bagian ini.

Penjawab aplikasi **wajib model yang sama dengan yang dinilai** (`LLM_BASE_URL` + `LLM_MODEL`
menunjuk gateway `tokenharbor/deepseek-v4-flash:free`). Bila penjawab diganti, angka RAGAS di
bawah tidak lagi menggambarkan aplikasi dan FASE B harus dijalankan ulang. Embedding tetap
`gemini-embedding-001` pada kedua tahap.

Ringkasnya: FASE A (sapuan retrieval, tanpa LLM, deterministik) dipakai **memilih** `top_k` &
threshold; FASE B (RAGAS 0.3.9, 4 metrik, LLM judge) **menyatakan mutu jawaban** pada konfigurasi
terpilih. Keduanya dipakai karena RAGAS tidak punya metrik penolakan pertanyaan di luar cakupan dan
tidak dapat membedakan antar-`k` (derau penilai 0,04–0,08 lebih besar dari beda antarkonfigurasi).

Bank pertanyaan ada di `eval/pertanyaan*.jsonl` — 92 soal, tapi **yang dipakai mengukur mutu 50 soal
inti**: 25 parafrase, 15 sintesis, 10 negatif (sisanya 15 jebakan dan 27 pembanding, dikeluarkan dari
angka mutu). Jawaban acuan disalin dari dokumen `.md` (bukan hasil generate LLM). Pertanyaan negatif
sengaja **tanpa** potongan acuan — sistem dinilai benar kalau tidak menemukan apa pun.

```bash
# 1. muat bank soal + tentukan potongan acuan (tanpa LLM)
python eval/muat_pertanyaan.py --hapus

# 2. FASE A — sapuan top_k x threshold, 15 konfigurasi, nol panggilan LLM
python eval/fase_a_retrieval.py

# 3. pilih konfigurasi hasil FASE A, lalu jawab (LLM) — FASE B dinilai hanya kalau ada penilai
.venv\Scripts\python.exe eval/fase_b_ragas.py --config A-k8-t60 --answer
.venv\Scripts\python.exe eval/fase_b_ragas.py --config A-k8-t60 --score

# 4. laporan angka
python eval/laporan.py
```

Konfigurasi penilai (`.env`): `EVAL_JUDGE_BASE_URL` + `EVAL_JUDGE_MODEL` + `EVAL_JUDGE_API_KEY` —
default menunjuk gateway 9router (`http://homelab-jay:20128/v1`, model
`tokenharbor/deepseek-v4-flash:free`). Jangan pakai Gemini untuk penilaian (kuota habis & jalur
kodenya sudah dihapus). Kosongkan `EVAL_JUDGE_BASE_URL` untuk mematikan penilaian — `build_judge()`
menolak jalan kalau semua base URL kosong.

Angka FASE A yang tersimpan (k=8, 50 soal inti, satu baris per soal):

| threshold | recall | r_gold | presisi | token | top_neg | tolak negatif |
| --: | --: | --: | --: | --: | --: | --: |
| 0,50 | 0,975 | 0,900 | 0,219 | 631 | 0,761 | 0/10 |
| **0,60** (default) | 0,975 | 0,900 | 0,219 | 571 | 0,761 | 0/10 |
| 0,70 | 0,975 | 0,900 | 0,219 | 548 | 0,628 | 2/10 |
| 0,75 | 0,975 | 0,900 | 0,226 | 486 | 0,483 | 4/10 |
| 0,80 | 0,975 | 0,850 | 0,344 | 363 | 0,089 | 9/10 |
| 0,85 | 0,825 | 0,700 | 0,411 | 172 | 0,089 | 9/10 |
| 0,90 | 0,525 | 0,425 | 0,396 | 42 | 0,000 | 10/10 |

Bacaan: threshold 0,50 **tidak pernah lebih baik** dari 0,60 (identik, token malah naik). 0,60→0,75
mempertahankan recall & `r_gold` penuh sambil memangkas token 15 % dan menolak 4/10 pertanyaan di
luar cakupan. Kerugian mulai muncul di 0,80 (`r_gold` −0,05) dan rusak di ≥0,85. Titik seimbang
0,70–0,75; 0,60 tetap dipakai aplikasi karena paling aman untuk dokumen baru.

### Uji temperature 0,0 vs 0,3 (50 soal inti, konteks identik)

Retrieval kedua run identik (k=8, threshold 0,60); yang berubah hanya temperature penjawab. Angka =
rata-rata berpasangan pada soal yang bernilai di kedua konfigurasi (D-k8-t60 vs T-k8-t60).

| metrik | n | temp 0,0 | temp 0,3 | selisih |
| :-- | --: | --: | --: | --: |
| faithfulness | 50 | 0,843 | 0,881 | +0,038 |
| answer_relevancy | 50 | 0,643 | 0,704 | +0,061 |
| context_precision | 50 | 0,834 | 0,808 | **−0,026** |
| context_recall | 50 | 0,810 | 0,860 | **+0,050** |

`context_precision` memanggil penilai delapan kali per soal (satu per potongan konteks), jadi paling
lambat; beberapa potongan sempat gagal karena batas laju penilai gratis dan diulang sampai tuntas
50/50 (nilai NULL tidak ditulis, jadi tidak ada angka karangan).

Karena konteks yang dinilai identik, dua metrik konteks **seharusnya tidak berubah** — tetapi
bergerak −0,027 dan +0,050. Itu ukuran langsung derau penilai antar-run. Kesimpulan: **temperature
0,0 vs 0,3 tidak menghasilkan perbedaan yang bisa dibedakan dari derau penilai**; temperature 0,0
dipertahankan karena determinisme/reproduktibilitas, bukan karena skornya lebih tinggi.

FASE B pada konfigurasi aplikasi `D-k8-t60` (50 soal x 4 metrik, penilai
`tokenharbor/deepseek-v4-flash:free` via gateway 9router, satu proses): faithfulness 0,843;
answer_relevancy 0,643; context_precision 0,834; context_recall 0,810. Seluruh 200 baris nilai
(4 metrik x 50 soal) sudah diverifikasi berlabel penilai gateway — baris lama berlabel
`gemini-3.1-flash-lite` dihapus 12 Nov 2026. Selisih
antar-k di FASE B tidak bisa dibedakan dari derau penilai, jadi pilihan `top_k` bertumpu pada
FASE A.

Menyiapkan lingkungan evaluasi sekali saja:

```bash
uv venv .venv --python 3.12
uv pip install -r requirements.txt --python .venv/Scripts/python.exe
```

Satu berkas requirements untuk aplikasi + evaluasi (pin `ragas 0.3.9` + `langchain 0.3.27` wajib
pasangan itu; `ragas 0.4.x` + `langchain 1.x` gagal import). Penilai LLM memakai gateway
OpenAI-compatible, embedding metrik dihitung lokal lewat `fastembed` — kuota Gemini hanya untuk
aplikasi.

## Troubleshooting

| Gejala | Sebab | Solusi |
| :-- | :-- | :-- |
| `PostgreSQL tidak bisa dihubungi` | service mati | Laragon → menu PostgreSQL → Start |
| `relation "chunks" does not exist` | konek ke database/schema salah | cek `DATABASE_URL`; `SELECT current_database(), current_schema();` |
| `429 RESOURCE_EXHAUSTED`, `retryDelay` ~30–60 s | batas per menit | tunggu, jalankan ulang |
| `429`, `retryDelay` ribuan detik | kuota **harian** habis | tunggu reset (00:00 Pasifik = 16:00 WITA) |
| Bot bilang "tidak ditemukan" padahal datanya ada | retrieval meleset, bukan LLM | cek `rag.retrieval_logs`, turunkan `SIMILARITY_THRESHOLD` |
| Jawaban salah padahal dokumen benar | dokumennya yang salah | perbaiki `.md`, jalankan `ingest.py` |
| `ImportError: cannot import name 'genai'` | memakai `python` global | aktifkan venv dulu |
| `ModuleNotFoundError: langchain_community.chat_models.vertexai` | ragas 0.4.x + langchain 1.x tidak cocok | pakai pin di `requirements.txt` (ragas 0.3.9 + langchain 0.3.27) |
| `Permission denied for schema eval` | konek pakai `rag_app` (sengaja tidak punya hak `eval`) | pakai `EVAL_DATABASE_URL` (`rag_eval`) |
| RAGAS `Model ... is not supported in TextEmbedding` | nama model bukan milik fastembed | cek `TextEmbedding.list_supported_models()` |

**Catatan kuota:** batas free tier dihitung **per teks**, bukan per request HTTP. Satu panggilan
berisi 80 teks tetap dihitung 80 teks — memperbesar batch hanya menghemat jumlah request.

---

## Stack

| Komponen | Versi | Peran |
| :-- | :-- | :-- |
| Python | 3.10+ | bahasa |
| google-genai | 2.33.0 | SDK Gemini (embedding + generate) |
| Flask | 3.1.3 | server web |
| psycopg | 3.2.10 | driver PostgreSQL |
| PostgreSQL | 14.5 | basis data |
| pgvector | 0.8.7 | tipe `vector` + index HNSW |
| python-dotenv | 1.2.4 | membaca `.env` |

---

## Sumber Data

Isi `data/docs-kurikulum/` disusun dari situs resmi program studi dan institusi ITB STIKOM Bali.
Berkas profil institusi mencantumkan alamat `Sumber data:` beserta periode jabatan, sehingga data
yang cepat berubah (pejabat, akreditasi) bisa ditelusuri dan diperbarui.

---

## Langkah Berikutnya

Fondasi data **selesai**: 724 potongan, 9 dokumen (termasuk daftar 225 dosen & 158 karyawan),
satu backend (PostgreSQL + pgvector), dan `ingest.py` yang aman dijalankan ulang.
Pengukuran sudah pernah dijalankan penuh: `eval/fase_a_retrieval.py` (15 konfigurasi x 50 soal inti, gratis,
tanpa LLM) dan `eval/fase_b_ragas.py --config D-k8-t60 --answer --score` (50 soal x 4 metrik RAGAS).
Angka terverifikasi ada di bagian **Evaluasi** di atas; penilai RAGAS kini dimatikan di `.env`.
