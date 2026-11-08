import os
import json 
import numpy as np
from dotenv import load_dotenv
from pathlib import Path
from google import genai
from google.genai import types

load_dotenv()

client = genai.Client()
MODEL_LLM = os.getenv("MODEL_LLM")
MODEL_EMBED = os.getenv("MODEL_EMBED")
EMBED_DIM = int(os.getenv("EMBED_DIM"))
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD"))
TOP_K = int(os.getenv("TOP_K"))
INDEX_FILE = os.getenv("INDEX_FILE")


def get_embedding(text: str) -> list[float]:
    response = client.models.embed_content(
        model=MODEL_EMBED,
        contents=text,
        config={"output_dimensionality": EMBED_DIM}
    )
    return response.embeddings[0].values

def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))

def ask_question(question: str, top_k: int = TOP_K, threshold: float = SIMILARITY_THRESHOLD) -> dict:
    # load database dari file JSON
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            database = json.load(f)
            for item in database:
                item["vector"] = np.array(item["vector"])
    except FileNotFoundError:
        return {"answer": "Index belum dibuat. Jalankan ingest.py terlebih dahulu.", "sources": []}

    #2. vectorkan pertanyaan users
    query_vec = np.array(get_embedding(question))

    #3. hitung similarity antara pertanyaan users dengan semua chunk di database
    scored_items = []
    for item in database:
        score = cosine_similarity(query_vec, item["vector"])
        if score >= threshold:
            scored_items.append((score, item))

    #urutkan hasil berdasarkan skor similarity tertinggi
    scored_items.sort(key=lambda x: x[0], reverse=True)
    #buang chunk yang teksnya kembar (boilerplate sama di banyak file),
    seen_text, unique_items = set(), []
    for score, item in scored_items:
        key = item["text"].strip()
        if key in seen_text:
            continue
        seen_text.add(key)
        unique_items.append((score, item))
        if len(unique_items) == top_k:
            break
    top_matches = unique_items

    #jika tidak ada hasil yang memenuhi threshold, kembalikan pesan kosong
    if not top_matches:
        return {"question": question, "answer": "Maaf, informasi yang Anda cari tidak ditemukan.", "sources": []}

    #4. susun konteks untuk LLM
    context_blocks = []
    for i, (score, item) in enumerate(top_matches, 1):
        context_blocks.append(f"[{i}] ({item['source']}):\n{item['text']}")
    context_text = "\n\n".join(context_blocks)

    #5. buat prompt untuk LLM
    prompt = f"""Anda adalah Asisten Virtual Kurikulum & Akademik ITB STIKOM Bali.
    Tugas Anda adalah menjawab pertanyaan civitas akademika HANYA berdasarkan konteks dokumen kurikulum resmi di bawah.

    PANDUAN FORMAT JAWABAN:
    1. Berikan jawaban yang ramah, ringkas, dan to the point.
    2. Jika jawaban memuat daftar mata kuliah, prasyarat, atau semester, gunakan poin-poin (bullet points) agar mudah dibaca.
    3. JANGAN membuat asumsi, aturan baru, atau menebak jika tidak tertulis eksplisit di dokumen.
    4. Jika jawaban membuat list daftar mata kuliah, semester, sertakan informasi sks.

    KONTEKS DOKUMEN:
    {context_text}

    PERTANYAAN USER: {question}
    JAWABAN:"""


    response = client.models.generate_content(
        model=(MODEL_LLM),
        contents=prompt,
        config={"temperature": 0.0}
    )
    return {"answer": response.text, 
            "sources": [{"file": item["source"], "score": round(score, 3)} for score, item in top_matches]
    }