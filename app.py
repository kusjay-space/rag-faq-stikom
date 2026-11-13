import os

from flask import Flask, request, jsonify, render_template
from rag import ask_question

# template_folder="." artinya cari index.html di folder yang sama dengan app.py
app = Flask(__name__, template_folder=".")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/tanya", methods=["POST"])
def api_ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()[:500]
    if not question:
        return jsonify({"answer": "Pertanyaan kosong.", "sources": []}), 400

    result = ask_question(question)
    return jsonify(result)

if __name__ == "__main__":
    # DEBUG=1 di .env hanya untuk pengembangan lokal; JANGAN diaktifkan di server publik
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("DEBUG", "0") == "1",
    )
