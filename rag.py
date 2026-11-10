import os
import time
import db
from dotenv import load_dotenv
from google import genai


load_dotenv()

client = genai.Client()
MODEL_LLM = os.getenv("MODEL_LLM")
MODEL_EMBED = os.getenv("MODEL_EMBED")
EMBED_DIM = int(os.getenv("EMBED_DIM"))
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD"))
TOP_K = int(os.getenv("TOP_K"))

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
        model=MODEL_EMBED,
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


def generate(prompt: str, temperature: float = 0.0) -> str:
    response = client.models.generate_content(
        model=MODEL_LLM,
        contents=prompt,
        config={"temperature": temperature},
    )
    return response.text


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
