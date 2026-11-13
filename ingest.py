import hashlib
import os
import sys
import time
import db
from pathlib import Path
from dotenv import load_dotenv
from google import genai

load_dotenv(override=True)

client = genai.Client()

EMBED_MODEL = os.getenv("EMBED_MODEL")
EMBED_DIM = int(os.getenv("EMBED_DIM"))
DOCS_DIR = Path(os.getenv("DOCS_DIR"))
BATCH_SIZE = 80
BATCH_PAUSE = float(os.getenv("INGEST_JEDA_DETIK", "65"))
MIN_CHUNK_CHARS = 30


def get_embedding_batch(texts: list[str]) -> list[list[float]]:
    while True:
        try:
            response = client.models.embed_content(
                model=EMBED_MODEL,
                contents=texts,
                config={"output_dimensionality": EMBED_DIM},
            )
            return [embedding.values for embedding in response.embeddings]
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print("Kena limit kuota (429), menunggu 40 detik...")
                time.sleep(40)
            else:
                raise e


def split_chunks(path: Path) -> list[dict]:
    chunks = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        text = block.strip()
        if len(text) < MIN_CHUNK_CHARS or text.startswith("Sumber data:"):
            continue
        # buang judul gantung (heading tanpa isi), bukan heading+isinya
        if text.startswith("#") and len(text.splitlines()) == 1:
            continue
        chunks.append(text)
    return chunks


def embed_file(path: Path, sha256: str, size_bytes: int) -> int:
    """Ganti seluruh potongan berkas ini dengan hasil embedding terbaru."""
    texts = split_chunks(path)
    # sha256 ditulis kosong dulu lalu diisi paling akhir: penanda berkas sudah utuh di DB,
    # jadi ingest yang mati di tengah jalan akan diulang, bukan dilewati
    document_id = db.upsert_document(path.name, "", size_bytes)
    db.delete_chunks(document_id)

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        vectors = get_embedding_batch(batch)
        rows = [{"seq": i + j, "text": text, "vector": vector}
                for j, (text, vector) in enumerate(zip(batch, vectors))]
        db.save_batch(rows, document_id)
        if i + BATCH_SIZE < len(texts):
            time.sleep(BATCH_PAUSE)

    db.upsert_document(path.name, sha256, size_bytes)
    print(f"  {path.name}: {len(texts)} potongan (document_id={document_id})")
    return len(texts)


def main():
    force = "--force" in sys.argv
    files = sorted(DOCS_DIR.glob("**/*.md"))
    if not files:
        sys.exit(f"tidak ada berkas .md di {DOCS_DIR} — ingest dibatalkan, tidak ada yang dihapus")

    names = {p.name for p in files}
    known = db.document_state()

    changed, skipped = [], []
    for path in files:
        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        if not force and known.get(path.name, {}).get("sha256") == sha256:
            skipped.append(path.name)
        else:
            changed.append((path, sha256, len(raw)))
    removed = [name for name in known if name not in names]

    print(f"{len(files)} berkas di {DOCS_DIR}: {len(skipped)} tidak berubah, "
          f"{len(changed)} perlu di-embed, {len(removed)} sudah tidak ada di disk")
    if not changed and not removed:
        print("Tidak ada yang perlu dikerjakan — nol panggilan API.")
        return

    run_id = db.start_run(EMBED_MODEL, EMBED_DIM, BATCH_SIZE, 0)
    done = 0
    try:
        for path, sha256, size_bytes in changed:
            done += embed_file(path, sha256, size_bytes)
            db.update_run(run_id, done)
        for name in removed:
            document_id = known[name]["id"]
            db.delete_chunks(document_id)
            db.delete_document(document_id)
            print(f"  {name}: dihapus dari database ({known[name]['chunks']} potongan)")
    except BaseException as e:
        db.finish_run(run_id, "failed", done, str(e)[:300])
        raise

    db.finish_run(run_id, "completed", done)
    print(f"Selesai: {done} potongan dari {len(changed)} berkas di-embed. {db.stats()}")


if __name__ == "__main__":
    main()
