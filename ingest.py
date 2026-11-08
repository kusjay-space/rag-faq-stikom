import os
import json
import time
from dotenv import load_dotenv
from pathlib import Path
from google import genai

load_dotenv()

client = genai.Client()

MODEL_EMBED = os.getenv("MODEL_EMBED")
EMBED_DIM = int(os.getenv("EMBED_DIM"))
DOCS_DIR = Path(os.getenv("DOCS_DIR"))
INDEX_FILE = os.getenv("INDEX_FILE")
BATCH_SIZE = 80 #kirim 80 chunk per request ke API embedding

def get_embedding_batch(texts: list[str]) -> list[list[float]]:
    while True:
        try:
            response = client.models.embed_content(
                model=MODEL_EMBED,
                contents=texts,
                config={"output_dimensionality": EMBED_DIM}
            )
            return [embedding.values for embedding in response.embeddings]
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print("Kena limit kuota (429), menunggu 40 detik...")
                time.sleep(40)
            else:
                raise e

def main():
    files = list(DOCS_DIR.glob("**/*.md"))
    print(f"Ingest {len(files)} file dari {DOCS_DIR}...")
    all_chunks = []

    for file in files:
        content = file.read_text(encoding="utf-8")
        for block in content.split("\n\n"):
            text = block.strip()
            if len(text) < 30 or text.startswith("Sumber data:"):
                continue
            # buang HANYA judul gantung (heading tanpa isi), bukan heading+isinya
            if text.startswith("#") and len(text.splitlines()) == 1:
                continue

            all_chunks.append({
                "source": file.name,
                "text": text,
            })

    total = len(all_chunks)
    print(f"Total {total} chunk akan di-embed dan disimpan ke {INDEX_FILE}. Memulai batch embedding...")
    database = []
    # proses batch embedding, setiap batch berisi BATCH_SIZE (80) chunk
    for i in range(0, total, BATCH_SIZE):
        batch = all_chunks[i:i + BATCH_SIZE]
        batch_texts = [item["text"] for item in batch]
        #panggil API 1x untuk 80 chunk sekaligus
        vectors = get_embedding_batch(batch_texts)
        for item, vector in zip(batch, vectors):
            database.append({
                "source": item["source"],
                "text": item["text"],
                "vector": vector
            })
        print(f"Selesai {len(database)}/{total} chunk.")
        time.sleep(65)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(database, f, indent=2)
    print(f"Selesai: {len(database)} chunk tersimpan di {INDEX_FILE}")

if __name__ == "__main__":
    main()
