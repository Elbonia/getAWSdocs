"""AWS Documentation RAG CLI using LlamaIndex & PostgreSQL pgvector."""

import argparse
import logging
import os
import sys
import time
from typing import List, Optional
import warnings

import llama_index.llms.anthropic.utils as anthropic_utils
from llama_index.core import (
    Settings,
    SimpleDirectoryReader,
    StorageContext,
    VectorStoreIndex,
)
from llama_index.core.llms import MockLLM
from llama_index.core.node_parser import SentenceWindowNodeParser
from llama_index.core.postprocessor import MetadataReplacementPostProcessor
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import NodeWithScore, QueryBundle
from llama_index.embeddings.google_genai import GoogleGenAIEmbedding
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.llms.anthropic import Anthropic
from llama_index.llms.google_genai import GoogleGenAI
from llama_index.vector_stores.postgres import PGVectorStore
import psycopg2


class FileDeduplicationPostprocessor(BaseNodePostprocessor):
    """Postprocessor that ensures file diversity by limiting duplicate chunks from the same file."""

    max_chunks_per_file: int = 1

    @classmethod
    def class_name(cls) -> str:
        """Return the class name identifier."""
        return "FileDeduplicationPostprocessor"

    def _postprocess_nodes(
        self,
        nodes: List[NodeWithScore],
        query_bundle: Optional[QueryBundle] = None,
    ) -> List[NodeWithScore]:
        """Filter nodes to maintain maximum chunk diversity per source file."""
        seen_files = {}
        deduped = []
        for n in nodes:
            fp = n.node.metadata.get("file_path", "?")
            cnt = seen_files.get(fp, 0)
            if cnt < self.max_chunks_per_file:
                seen_files[fp] = cnt + 1
                deduped.append(n)
        return deduped


anthropic_utils.CLAUDE_MODELS["claude-3-5-sonnet-20241022"] = 200000

# LLMs used to synthesize answers during `query`
GEMINI_LLM = "gemini-2.5-flash"
CLAUDE_LLM = "claude-3-5-sonnet-20241022"


# Database connection details
DB_NAME = "aws_rag"
DB_USER = "postgres"
DB_PASSWORD = "mysecretpassword"
DB_HOST = "localhost"
DB_PORT = 5433

# Model mapping
MODELS = {
    "qwen3-0.6b": {
        "name": "Qwen/Qwen3-Embedding-0.6B",
        "dim": 1024,
        "type": "local",
        "table": "aws_docs_qwen3_0_6b",
        "query_instruction": (
            "Instruct: Given a web search query, retrieve relevant passages "
            "that answer the query\nQuery: "
        ),
        "torch_dtype": "bfloat16",
        "max_length": 1024,
        "embed_batch_size": 16,
        "trust_remote_code": False,
    }
}


def get_gemini_api_key():
    """Return the Gemini key, accepting either name the google-genai SDK honors."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print(
            "Error: neither GEMINI_API_KEY nor GOOGLE_API_KEY is set.", file=sys.stderr
        )
        print(
            "Please set one in your terminal before running this script:",
            file=sys.stderr,
        )
        print('  export GEMINI_API_KEY="your_api_key_here"', file=sys.stderr)
        sys.exit(1)
    return key


def get_anthropic_api_key():
    """Return the Anthropic key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Error: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        print(
            "Please set it in your terminal before running with --llm claude:",
            file=sys.stderr,
        )
        print('  export ANTHROPIC_API_KEY="your_api_key_here"', file=sys.stderr)
        sys.exit(1)
    return key


def get_vector_store(model_choice, table_suffix=""):
    """Return the pgvector store for a model.

    `table_suffix` selects a separate corpus in the same database: the S3
    eval baseline lives in the unsuffixed tables, so indexing another
    document set with a suffix leaves that baseline intact.
    """
    if model_choice not in MODELS:
        print(f"Error: Model '{model_choice}' is not supported.", file=sys.stderr)
        print(f"Supported models: {list(MODELS.keys())}", file=sys.stderr)
        sys.exit(1)

    model_info = MODELS[model_choice]
    table_name = model_info["table"] + table_suffix

    # Setup connection
    conn = psycopg2.connect(
        dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=DB_PORT
    )
    conn.autocommit = True

    # Initialize extension
    with conn.cursor() as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    conn.close()

    # Create PGVectorStore
    vector_store = PGVectorStore.from_params(
        database=DB_NAME,
        host=DB_HOST,
        password=DB_PASSWORD,
        port=DB_PORT,
        user=DB_USER,
        table_name=table_name,
        embed_dim=model_info["dim"],
    )
    return vector_store, table_name


def silence_extended_attention_mask_notice():
    """Hide the `get_extended_attention_mask` deprecation notice.

    nomic-ai's `trust_remote_code` modeling file calls that deprecated
    transformers helper itself, so the notice is not actionable here. It is
    filtered by message so unrelated transformers warnings still surface.
    """
    needle = "get_extended_attention_mask"
    warnings.filterwarnings("ignore", message=f".*{needle}.*")

    class DropNotice(logging.Filter):
        """Filter to suppress deprecation log messages for extended attention mask."""

        def filter(self, record):
            """Filter out records matching the target deprecation message."""
            return needle not in record.getMessage()

    drop = DropNotice()
    root = logging.getLogger("transformers")
    root.addFilter(drop)
    # Child loggers (e.g. transformers.modeling_utils) bypass a logger-level
    # filter on the parent, but their records still reach the parent handler.
    for handler in root.handlers or logging.getLogger().handlers:
        handler.addFilter(drop)


def setup_settings(model_choice, is_query=False, llm_choice="gemini"):
    """Configure global LlamaIndex Settings for LLM and embedding models."""
    model_info = MODELS[model_choice]

    # Initialize LLM only during queries (indexing doesn't need an LLM)
    if is_query:
        if llm_choice == "gemini":
            Settings.llm = GoogleGenAI(
                model=GEMINI_LLM,
                api_key=get_gemini_api_key(),
            )
        elif llm_choice == "claude":
            Settings.llm = Anthropic(
                model=CLAUDE_LLM,
                api_key=get_anthropic_api_key(),
            )
        else:
            print(
                f"Error: Unsupported LLM choice '{llm_choice}'. Supported LLMs: ['gemini', 'claude']",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Indexing has no LLM requirements; use a MockLLM
        Settings.llm = MockLLM()

    # Initialize Embedding Model
    if model_info["type"] == "api":
        Settings.embed_model = GoogleGenAIEmbedding(
            model_name=model_info["name"],
            api_key=get_gemini_api_key(),
        )
    else:
        # Load local model using HuggingFace
        print(f"Loading local embedding model: '{model_info['name']}'...")
        silence_extended_attention_mask_notice()
        # Per-model tuning knobs, applied only when the model defines them.
        hf_kwargs = {
            key: model_info[key]
            for key in (
                "query_instruction",
                "text_instruction",
                "max_length",
                "embed_batch_size",
            )
            if key in model_info
        }
        if "torch_dtype" in model_info:
            hf_kwargs["model_kwargs"] = {"torch_dtype": model_info["torch_dtype"]}

        Settings.embed_model = HuggingFaceEmbedding(
            model_name=model_info["name"],
            trust_remote_code=model_info.get("trust_remote_code", True),
            **hf_kwargs,
        )


def cmd_index(
    directory,
    clean=False,
    update=False,
    model="qwen3-0.6b",
    table_suffix="",
    reuse_settings=False,
):
    """Index markdown documentation files from a target directory into PostgreSQL."""
    # Batch drivers load the embedding model once and index many directories,
    # so they opt out of the per-call setup that would reload it each time.
    if not reuse_settings:
        setup_settings(model)
    vector_store, table_name = get_vector_store(model, table_suffix)

    if clean:
        print(
            f"Clearing existing database table 'data_{table_name}' to avoid duplicates..."
        )
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute(f"DROP TABLE IF EXISTS data_{table_name} CASCADE;")
        conn.close()

        # Reinitialize vector store after drop
        vector_store, table_name = get_vector_store(model, table_suffix)
    elif update:
        abs_target = os.path.abspath(directory)
        print(
            f"Updating database: Pruning existing records for '{directory}' in 'data_{table_name}'..."
        )
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s);",
                (f"data_{table_name}",),
            )
            if c.fetchone()[0]:
                c.execute(
                    f"DELETE FROM data_{table_name} WHERE metadata_->>'file_path' LIKE %s OR metadata_->>'file_path' = %s;",
                    (f"{abs_target}/%", abs_target),
                )
                print(f"Pruned {c.rowcount} previous chunk records.")
        conn.close()

    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    if not os.path.exists(directory):
        print(f"Error: Directory '{directory}' does not exist.", file=sys.stderr)
        sys.exit(1)

    print(f"Reading markdown files in '{directory}'...")
    exclude_patterns = [
        "**/venv/**",
        "**/.venv/**",
        "**/rag_system/**",
        "**/node_modules/**",
    ]
    reader = SimpleDirectoryReader(
        input_dir=directory,
        recursive=True,
        required_exts=[".md"],
        exclude=exclude_patterns,
    )
    documents = reader.load_data()
    print(f"Loaded {len(documents)} document pages.")

    if not documents:
        print("No markdown (.md) files found to index.")
        return

    print("Parsing documents into sentence window nodes (window_size=3)...")
    node_parser = SentenceWindowNodeParser.from_defaults(
        window_size=3,
        window_metadata_key="window",
        original_text_metadata_key="original_text",
    )
    nodes = node_parser.get_nodes_from_documents(documents)

    print(f"Generating embeddings using '{model}' and indexing to PostgreSQL...")
    VectorStoreIndex(nodes, storage_context=storage_context, show_progress=True)
    print("Successfully indexed and saved to database!")


def cmd_query(
    query_text, num_results=5, model="qwen3-0.6b", table_suffix="", llm="gemini"
):
    """Query the indexed vector database and synthesize an answer using the chosen LLM."""
    setup_settings(model, is_query=True, llm_choice=llm)
    vector_store, _ = get_vector_store(model, table_suffix)

    # Load index from the vector store
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store)

    # Retrieve a larger candidate pool, deduplicate per file, and expand sentence windows
    candidate_k = num_results * 4
    dedup_processor = FileDeduplicationPostprocessor(max_chunks_per_file=1)
    window_processor = MetadataReplacementPostProcessor(target_metadata_key="window")
    query_engine = index.as_query_engine(
        similarity_top_k=candidate_k,
        node_postprocessors=[dedup_processor, window_processor],
    )

    print(
        f"Querying using '{model}' embedding model and '{llm}' LLM: \"{query_text}\"...\n"
    )
    start_time = time.time()
    response = query_engine.query(query_text)
    latency = time.time() - start_time

    print("--- ANSWER ---")
    print(response)
    print("\n--- SOURCES ---")
    for source in response.source_nodes:
        metadata = source.node.metadata
        file_path = metadata.get("file_path", "Unknown")
        score = source.score if source.score is not None else 0.0
        # Show the service-qualified path: basenames repeat across services
        # (Welcome.md exists in 7 of them), so a bare name is ambiguous.
        marker = "/markdown/"
        idx = file_path.find(marker)
        label = (
            file_path[idx + len(marker) :] if idx >= 0 else os.path.basename(file_path)
        )
        print(f"- {label} (Score: {score:.4f})")
    print(f"\nQuery Latency: {latency:.2f} seconds")


def main():
    """Parse command line arguments and execute index or query subcommands."""
    parser = argparse.ArgumentParser(
        description="AWS Documentation RAG CLI using LlamaIndex & PostgreSQL pgvector"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Index parser
    index_parser = subparsers.add_parser(
        "index", help="Index markdown files from a directory into the database"
    )
    index_parser.add_argument(
        "directory", help="The absolute or relative directory path to index"
    )
    index_parser.add_argument(
        "--clean",
        action="store_true",
        help="Clear existing index before indexing new files",
    )
    index_parser.add_argument(
        "--update",
        action="store_true",
        help="Replace existing DB records for target files without wiping the entire table",
    )
    index_parser.add_argument(
        "--model",
        default="qwen3-0.6b",
        choices=list(MODELS.keys()),
        help="Embedding model to use",
    )
    index_parser.add_argument(
        "--table-suffix",
        default="",
        help="Index/query a separate corpus in its own tables (e.g. --table-suffix _multi)",
    )

    # Query parser
    query_parser = subparsers.add_parser(
        "query", help="Query the indexed database for answers"
    )
    query_parser.add_argument("text", help="The query text / question")
    query_parser.add_argument(
        "--limit", type=int, default=5, help="Number of context documents to retrieve"
    )
    query_parser.add_argument(
        "--model",
        default="qwen3-0.6b",
        choices=list(MODELS.keys()),
        help="Embedding model to use",
    )
    query_parser.add_argument(
        "--llm",
        default="gemini",
        choices=["gemini", "claude"],
        help="LLM for synthesis: 'gemini' or 'claude'",
    )
    query_parser.add_argument(
        "--table-suffix",
        default="",
        help="Index/query a separate corpus in its own tables (e.g. --table-suffix _multi)",
    )

    args = parser.parse_args()

    if args.command == "index":
        cmd_index(
            args.directory, args.clean, args.update, args.model, args.table_suffix
        )

    elif args.command == "query":
        cmd_query(args.text, args.limit, args.model, args.table_suffix, args.llm)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
