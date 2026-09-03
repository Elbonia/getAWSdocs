# AWS Documentation RAG Pipeline

This project contains a local RAG (Retrieval-Augmented Generation) pipeline for querying AWS documentation using **LlamaIndex**, **PostgreSQL with `pgvector`**, and **Gemini**.

## Setup & Infrastructure

### 1. Database (Docker Compose)
We built a custom PostgreSQL image from `postgres:latest` that installs `pgvector` automatically, and set up persistent storage via a Docker volume so it survives host reboots.

The configurations are located in the `rag_system/` folder:
* **[Dockerfile](./Dockerfile)**: Builds the Postgres container with `postgresql-18-pgvector`.
* **[docker-compose.yml](./docker-compose.yml)**: Defers lifecycle management to Docker, restarts on failure or reboot (`restart: unless-stopped`), and stores data in a named volume (`pgdata`).

Start it with:
```bash
cd rag_system
docker compose up -d --build
```

Once up, the database listens on `localhost:5433` with:
* **Database**: `aws_rag`
* **Username**: `postgres`
* **Password**: `mysecretpassword`

These same values are hard-coded at the top of `rag_app.py`.

> If `docker` gives a "permission denied ... /var/run/docker.sock" error, add yourself to the
> `docker` group (`sudo usermod -aG docker $USER`) and start a new login shell, or prefix the
> compose command with `sudo`.

---

## How to Run

Before running the commands, navigate to the `rag_system/` directory:
```bash
cd rag_system
```

### 1. Create & Activate the Python Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(`.venv/` is git-ignored, so it has to be rebuilt on each machine. The huggingface/torch
dependencies are only needed for the local `--model` options; a Gemini-only setup can skip
`llama-index-embeddings-huggingface` and `einops`.)

### 2. Set your Gemini API Key
Required for `--model gemini` and for the answer-synthesis step of every `query` (the LLM is
`gemini-2.5-flash` via the `google-genai` SDK). Not needed to `index` with a local model.
```bash
export GEMINI_API_KEY="your_actual_api_key"
```
(`GOOGLE_API_KEY` is accepted as a fallback, since the `google-genai` SDK honors both names.)

### 3. Compare & Index with Different Embedding Models

The markdown corpus produced by `getAWSdocs.py` lives one level up from `rag_system/`:

* `../documentation/markdown/<guide>/...` — per-page markdown for each AWS guide (`./getAWSdocs.py -d md`)
* `../whitepapers/*.md` — AWS whitepaper sections (`./getAWSdocs.py -w`)

Point `index` at whichever subtree you want. The full `../documentation/markdown` tree is ~125k files, so for a first run pick a single guide, e.g. `../documentation/markdown/AmazonS3`.

You can run indexing using different models. The script automatically stores each model's embeddings in a separate table (`aws_docs_gemini`, `aws_docs_bge_m3`, `aws_docs_nomic`, `aws_docs_qwen_7b`, `aws_docs_qwen_1_5b`), allowing you to index and query them side-by-side to compare.

### 3. Indexing Documentation

The primary embedding model is **`qwen3-0.6b`** (`Qwen/Qwen3-Embedding-0.6B`), configured with native **`bfloat16`** precision and batch size 16 for high accuracy and sub-100ms retrieval.

**Example: Indexing Amazon S3 Docs**
```bash
python rag_app.py index ../documentation/markdown/AmazonS3
```

*(Note: Use `--update` to replace existing database records for specific files/directories without wiping the table, e.g. `python rag_app.py index ../documentation/markdown/AmazonS3 --update`)*
*(Note: Use `--clean` if you want to drop the table completely and start fresh, e.g. `python rag_app.py index ../documentation/markdown/AmazonS3 --clean`)*


### 4. Querying

You can query your index using either **Gemini** (default) or **Claude 3.5 Sonnet**:

```bash
# Query using Gemini
python rag_app.py query "How do I configure S3 Bucket owner preferred ownership?" --llm gemini

# Query using Claude 3.5 Sonnet
export ANTHROPIC_API_KEY="your_api_key"
python rag_app.py query "How do I configure S3 Bucket owner preferred ownership?" --llm claude
```



