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
  prompt (TOP_K potongan + nomor + nama berkas)  ──►  Gemini LLM (temperature 0.0)
        │
        ▼
  { "answer": ..., "sources": [{file, score}, ...] }  ──►  Flask /api/tanya

EVALUASI (terpisah, schema `eval`)                       lihat RENCANA-RAGAS.md
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
├── sql/schema.sql              # skema lengkap dari nol (runtime + evaluasi + ekstensi + hak akses)
├── requirements.txt
├── .env.example                # contoh konfigurasi (aman di-commit)
├── .env                        # konfigurasi asli + API key (TIDAK di-commit)
├── README.md                   # berkas ini
├── RENCANA-RAGAS.md            # rencana evaluasi (langkah berikutnya)
├── data/docs-kurikulum/        # SUMBER KEBENARAN — 7 dokumen .md
└── index.json                  # artefak lama sebelum pindah ke PostgreSQL; tidak dipakai kode
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
- Tabel `eval.*` **kosong sampai evaluasi dijalankan** (`RENCANA-RAGAS.md`).

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
| Ganti `MODEL_EMBED`/`EMBED_DIM`/aturan chunking | `--force` untuk embed ulang semua | seluruh korpus |

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
| `GEMINI_API_KEY` | — | Kunci API Google Gemini. **Jangan pernah di-commit.** |
| `MODEL_EMBED` | `gemini-embedding-001` | Model pengubah teks jadi vektor |
| `MODEL_LLM` | `gemini-3.1-flash-lite` | Model penyusun jawaban (dipakai saat `LLM_BASE_URL` kosong) |
| `LLM_BASE_URL` | — | Endpoint OpenAI-compatible untuk LLM, mis. `http://homelab-jay:20128/v1`. **Kosong = langsung ke Gemini** |
| `LLM_API_KEY` | — | Kunci gateway (tidak dipakai kalau `LLM_BASE_URL` kosong) |
| `LLM_MODEL` | — | Id model di gateway, mis. `experientiallabs/deepseek-v4-flash`; kosong = pakai `MODEL_LLM` |
| `EMBED_DIM` | `768` | Panjang vektor; **wajib sama** saat ingest dan query |
| `DOCS_DIR` | `data/docs-kurikulum` | Folder dokumen sumber |
| `SIMILARITY_THRESHOLD` | `0.60` | Ambang skor; di bawah ini potongan dibuang |
| `TOP_K` | `8` | Jumlah potongan konteks yang dikirim ke LLM |
| `DATABASE_URL` | — | Koneksi PostgreSQL; password hanya di `.env` |
| `INGEST_JEDA_DETIK` | `65` | Jeda antar batch 80 teks saat ingest (hindari `429`) |

Mengubah `EMBED_DIM` atau `MODEL_EMBED` membuat vektor lama tidak kompatibel — jalankan `ingest.py`
dari nol (untuk uji cepat tanpa jeda: `INGEST_JEDA_DETIK=0 python ingest.py`).

### Model LLM: langsung ke Gemini atau lewat gateway

`rag.generate()` punya dua jalur, dipilih otomatis dari `LLM_BASE_URL`:

| `LLM_BASE_URL` | Yang dipakai | Model |
| :-- | :-- | :-- |
| kosong | SDK `google-genai` | `MODEL_LLM` |
| terisi (mis. 9router) | HTTP `POST {base}/chat/completions` pakai pustaka bawaan Python | `LLM_MODEL` (fallback `MODEL_LLM`) |

Poin penting:

- **Embedding tidak ikut berpindah.** Untuk gateway OpenAI-compatible, vektor tetap dari
  `MODEL_EMBED` (Gemini) — jadi DB tidak perlu di-ingest ulang.
- Sebagian gateway menempelkan penanda SSE `data: [DONE]` di ekor respons non-stream; `rag.py`
  membaca objek JSON pertama (`raw_decode`) supaya tidak gagal parse. Ini pernah bikin error
  `JSONDecodeError: Extra data`.
- Mengganti model LLM mengubah isi jawaban. Kalau tabel evaluasi dibandingkan, **satu tabel = satu
  model**; jangan campur jawaban dua model tanpa menandainya.

---

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

Fondasi data **selesai**: 666 potongan, 7 dokumen, satu backend (PostgreSQL + pgvector), dan
`ingest.py` yang aman dijalankan ulang. Tahap berikutnya evaluasi kuantitatif: lihat `RENCANA-RAGAS.md`.
