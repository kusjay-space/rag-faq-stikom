import json
import os
import time
import urllib.request
import db
from dotenv import load_dotenv
from google import genai


load_dotenv(override=True)

client = genai.Client()
GEMINI_LLM_MODEL = os.getenv("GEMINI_LLM_MODEL")
EMBED_MODEL = os.getenv("EMBED_MODEL")
EMBED_DIM = int(os.getenv("EMBED_DIM"))
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD"))
TOP_K = int(os.getenv("TOP_K"))
# terisi = LLM lewat endpoint OpenAI-compatible (gateway/9router); kosong = langsung ke Gemini
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

PROMPT_TEMPLATE = """Anda adalah Asisten Virtual Kurikulum & Akademik ITB STIKOM Bali.
    Tugas Anda adalah menjawab pertanyaan civitas akademika HANYA berdasarkan konteks dokumen kurikulum resmi di bawah.

    PANDUAN FORMAT JAWABAN:
    1. Berikan jawaban yang ramah, ringkas, dan to the point.
    2. Jika jawaban memuat daftar mata kuliah, prasyarat, atau semester, gunakan poin-poin (bullet points) agar mudah dibaca.
    3. JANGAN membuat asumsi, aturan baru, atau menebak jika tidak tertulis eksplisit di dokumen.
    4. Jika jawaban membuat list daftar mata kuliah, semester, sertakan informasi sks.

    KONTEKS DOKUMEN:
    {context}

    PERTANYAAN USER: {question}
    JAWABAN:"""


def get_embedding(text: str) -> list[float]:
    response = client.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config={"output_dimensionality": EMBED_DIM},
    )
    return response.embeddings[0].values


def retrieve(question: str, top_k: int = TOP_K,
             threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    candidates = db.fetch_candidates(get_embedding(question), threshold)

    # buang teks kembar, ambil top_k teratas
    seen, matches = set(), []
    for item in candidates:
        key = item["text"].strip()
        if key in seen:
            continue
        seen.add(key)
        matches.append(item)
        if len(matches) == top_k:
            break
    return matches


def build_prompt(question: str, items: list[dict]) -> str:
    context = "\n\n".join(f"[{i}] ({it['source']}):\n{it['text']}" for i, it in enumerate(items, 1))
    return PROMPT_TEMPLATE.format(context=context, question=question)


def _clean_text(teks: str | None) -> str:
    """Buang pagar kode yang kadang ditambahkan model gateway, lalu rapikan spasi."""
    if not teks:
        return ""
    isi = teks.strip()
    if isi.startswith("```"):
        baris = isi.splitlines()
        if baris and baris[0].startswith("```"):
            baris = baris[1:]
        if baris and baris[-1].strip().startswith("```"):
            baris = baris[:-1]
        isi = "\n".join(baris).strip()
    return isi


def generate(prompt: str, temperature: float = 0.0) -> str:
    """Jawaban LLM. Jawaban kosong dianggap gagal (raise) supaya pemanggil bisa mencoba ulang,
    bukan tersimpan sebagai jawaban kosong yang lalu merusak penilaian RAGAS."""
    for percobaan in range(2):
        jawaban = _generate_once(prompt, temperature)
        if jawaban:
            return jawaban
        if percobaan == 0:
            time.sleep(2)
    raise RuntimeError("model mengembalikan jawaban kosong")


def _generate_once(prompt: str, temperature: float) -> str:
    if not LLM_BASE_URL:
        response = client.models.generate_content(
            model=GEMINI_LLM_MODEL,
            contents=prompt,
            config={"temperature": temperature},
        )
        return _clean_text(response.text)

    payload = json.dumps({
        "model": LLM_MODEL or GEMINI_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        body = response.read().decode("utf-8", "replace")
    # sebagian gateway menempelkan penanda SSE ("data: [DONE]") di ekor respons
    isi = json.JSONDecoder().raw_decode(body.lstrip())[0]["choices"][0]["message"]["content"]
    return _clean_text(isi)


def ask_question(question: str, top_k: int = TOP_K,
                 threshold: float = SIMILARITY_THRESHOLD) -> dict:
    started = time.perf_counter()
    matches = retrieve(question, top_k=top_k, threshold=threshold)
    db.log_retrieval(question, matches, int((time.perf_counter() - started) * 1000), top_k, threshold)

    if not matches:
        return {"question": question,
                "answer": "Maaf, informasi yang Anda cari tidak ditemukan.",
                "sources": []}

    return {
        "answer": generate(build_prompt(question, matches)),
        "sources": [{"file": it["source"], "score": round(it["score"], 3)} for it in matches],
    }
