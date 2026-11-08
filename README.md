# RAG FAQ Kurikulum — ITB STIKOM Bali

Chatbot akademik berbasis **Retrieval-Augmented Generation (RAG)**. Pertanyaan pengguna dijawab
**hanya** dari dokumen resmi kurikulum dan profil institusi yang ada di `data/docs-kurikulum/`,
lengkap dengan daftar sumber dan skor kemiripannya.

Tidak ada model yang di-fine-tune. Seluruh pengetahuan sistem berasal dari dokumen teks biasa
yang bisa diedit siapa pun tanpa menyentuh kode.

---

## Fitur

| Fitur | Keterangan |
| :-- | :-- |
| Pencarian semantik | Pakai embedding, jadi cocok berdasarkan **makna**, bukan kecocokan kata persis |
| Jawaban bergrounding | Model dilarang menjawab di luar konteks dokumen |
| Transparansi sumber | Setiap jawaban menyertakan file sumber + skor kemiripan |
| Tanpa basis data | Vektor disimpan di satu file `index.json`, pencarian pakai NumPy |
| UI siap pakai | Satu halaman HTML, tanpa build step |

---

## Arsitektur

```
                        TAHAP 1 — INGEST (sekali, dijalankan manual)
  data/docs-kurikulum/*.md
        │
        ├─ potong jadi chunk (per paragraf, "\\n\\n")
        ├─ buang chunk sampah (terlalu pendek / heading tanpa isi / baris metadata)
        └─ embed tiap chunk  ──►  Google Gemini Embedding API
                                      │
                                      ▼
                                 index.json      (666 chunk, vektor 768 dimensi)

                        TAHAP 2 — QUERY (tiap kali user bertanya)
  pertanyaan user
        │
        ├─ embed pertanyaan          ──►  vektor query (768 dimensi)
        ├─ hitung cosine similarity ke SEMUA chunk di index.json
        ├─ buang yang skornya < 0.60 (threshold)
        ├─ urutkan, buang chunk berisi teks kembar, ambil 8 teratas (top_k)
        │
        └─ susun prompt: instruksi + 8 potongan konteks + pertanyaan
                    │
                    ▼
              Gemini LLM  (temperature 0.0)
                    │
                    ▼
        { "answer": "...", "sources": [ {file, score}, ... ] }
```

---

## Struktur Folder

```
rag-faq-stikom/
├── app.py                        # server Flask + endpoint POST /api/tanya
├── rag.py                        # mesin retrieval + pemanggilan LLM
├── ingest.py                     # pemotongan dokumen + pembuatan index.json
├── index.html                    # antarmuka chat (HTML + CSS + JS, tanpa build)
├── requirements.txt
├── .env.example                  # contoh konfigurasi (aman untuk di-commit)
├── .env                          # konfigurasi asli + API key  (TIDAK di-commit)
│
├── data/
│   └── docs-kurikulum/           # SUMBER KEBENARAN — 7 dokumen .md
│       ├── profil-umum-stikom-bali.md
│       ├── daftar-prodi-stikom-bali.md
│       ├── kurikulum-ti-s1-teknologi-informasi.md
│       ├── kurikulum-si-s1-sistem-informasi.md
│       ├── kurikulum-sk-s1-sistem-komputer.md
│       ├── kurikulum-bd-s1-bisnis-digital.md
│       └── kurikulum-mi-d3-manajemen-informatika.md
│
└── index.json                    # ARTEFAK TURUNAN — hasil ingest.py (bisa dibangun ulang)
```

**Aturan penting:** `data/docs-kurikulum/` adalah sumber kebenaran. `index.json` hanya hasil
olahan. Kalau dokumen diperbaiki, index **harus** dibangun ulang agar jawaban ikut berubah.

---

## Cara Menjalankan

### 1. Siapkan environment

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
```

### 2. Konfigurasi

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # Linux / macOS
```

Lalu isi `GEMINI_API_KEY` di `.env` dengan API key dari
<https://aistudio.google.com/apikey>.

### 3. Bangun index

```bash
python ingest.py
```

Proses ini memanggil API embedding satu kali per chunk. Untuk 666 chunk, selesaikan dalam
beberapa menit. Perhatikan batas kuota gratis — lihat bagian Troubleshooting.

### 4. Jalankan

```bash
python app.py
```

Buka <http://127.0.0.1:5000>. Endpoint API: `POST /api/tanya` dengan body `{"question": "..."}`.

---

## Konfigurasi (`.env`)

| Variabel | Default | Fungsi |
| :-- | :-- | :-- |
| `GEMINI_API_KEY` | — | Kunci API Google Gemini. **Jangan pernah di-commit.** |
| `MODEL_EMBED` | `gemini-embedding-001` | Model untuk mengubah teks jadi vektor |
| `MODEL_LLM` | `gemini-3.1-flash-lite` | Model yang menyusun jawaban akhir |
| `EMBED_DIM` | `768` | Panjang vektor. **Wajib sama** saat ingest dan saat query |
| `DOCS_DIR` | `data/docs-kurikulum` | Folder dokumen sumber |
| `INDEX_FILE` | `index.json` | Lokasi file index |
| `SIMILARITY_THRESHOLD` | `0.60` | Ambang batas skor; di bawah ini chunk dibuang |
| `TOP_K` | `8` | Jumlah potongan konteks yang dikirim ke LLM |

Mengubah `EMBED_DIM` atau `MODEL_EMBED` membuat vektor lama tidak kompatibel — index harus
dibangun ulang dari nol.

---

## Troubleshooting

| Gejala | Sebab | Solusi |
| :-- | :-- | :-- |
| `429 RESOURCE_EXHAUSTED`, `retryDelay` ~30-60s | batas per menit | tunggu, jalankan ulang |
| `429`, `retryDelay` ribuan detik | kuota **harian** habis | tunggu reset (00:00 waktu Pasifik = 16:00 WITA) |
| Bot bilang "informasi tidak ditemukan" padahal datanya ada | retrieval gagal, bukan LLM | cek isi `index.json`, lihat skor, turunkan threshold |
| Jawaban salah padahal dokumen benar | dokumennya memang salah | perbaiki `.md`, bangun ulang index |
| `ImportError: cannot import name 'genai'` | menjalankan `python` global, bukan venv | aktifkan venv dulu |

**Catatan kuota:** batas free tier dihitung **per teks**, bukan per request HTTP. Satu panggilan
berisi 80 teks tetap dihitung 80. Membesarkan batch hanya menghemat jumlah request, bukan kuota.

---

## Catatan Teknis

- `data/docs-kurikulum/` adalah **sumber kebenaran**; `index.json` hanya hasil olahan.
  Memperbaiki jawaban berarti memperbaiki dokumen lalu membangun ulang index.
- Menambah dokumen: taruh berkas `.md` baru di `data/docs-kurikulum/`, lalu jalankan `python ingest.py`.
- Mengubah `EMBED_DIM` atau `MODEL_EMBED` membatalkan seluruh index lama — bangun ulang dari nol.
- Dokumen sebaiknya mencantumkan `Sumber data:` dan tanggal, supaya data yang cepat berubah
  (pejabat, akreditasi) bisa ditelusuri dan diperbarui.

---

## Stack

| Komponen | Versi | Peran |
| :-- | :-- | :-- |
| Python | 3.10+ | bahasa |
| google-genai | 2.33.0 | SDK Gemini (embedding + generate) |
| Flask | 3.1.3 | server web |
| NumPy | 2.2.6 | perhitungan cosine similarity |
| python-dotenv | 1.2.4 | membaca `.env` |

---

## Sumber Data

Isi `data/docs-kurikulum/` disusun dari situs resmi program studi dan institusi ITB STIKOM Bali.
Berkas profil institusi mencantumkan alamat `Sumber data:` beserta periode jabatan, sehingga
data yang cepat berubah (pejabat, akreditasi) bisa ditelusuri dan diperbarui.
