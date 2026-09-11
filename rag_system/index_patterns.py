#!/usr/bin/env python3
"""Index AWS Serverless Land patterns into dedicated PostgreSQL pgvector table.

Strategy:
- CloudFormation / AWS SAM templates (template.yaml, template.yml, template.json):
  Uses cfn_flip to split templates into atomic, self-contained Resource blocks
  (Type: AWS::Serverless::Function, AWS::SQS::Queue, etc.) with Parameters & Outputs blocks.
- Non-CloudFormation Code (CDK TypeScript/Python/Java, Terraform .tf, Python handlers, READMEs):
  Uses Qwen3-Embedding-0.6B with SentenceSplitter and Contextual Description Prefixing.
- Splitter (--splitter): defaults to `sentence` (SentenceSplitter, the original behavior).
  `code` routes each source file to an AST-aware CodeSplitter by language (falling back to
  the sentence splitter for markdown/JSON/CFN docs and any file that fails to parse).
  `code` mode requires the optional `tree-sitter-language-pack` dependency.
- Batch Processing & Progress Tracking: Saves progress in .patterns_index_progress.json.
- Logging: Real-time logging to stdout and index_patterns.log with ETA and throughput metrics.
"""

import argparse
import os
import sys
import time
import json
import re
import glob
import logging
from typing import Dict, Any, List

import cfn_flip
from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rag_app  # pylint: disable=wrong-import-position

BASE_PATTERNS_DIR = "/home/jmikhail/code/aws/serverless-patterns"
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".patterns_index_progress.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_patterns.log")
TABLE_SUFFIX = "patterns"
MODEL_NAME = "qwen3-0.6b"

# File extension -> tree-sitter-language-pack grammar name, used by --splitter code.
# Anything not listed (README.md, *.json, *.tfvars, CFN resource docs) falls back to
# the SentenceSplitter, which is also the default for every file when --splitter sentence.
CODE_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".java": "java",
    ".cs": "csharp",
    ".go": "go",
    ".graphql": "graphql",
    ".tf": "hcl",
}


def setup_logger() -> logging.Logger:
    """Configure logging to stdout and index_patterns.log file."""
    logger = logging.getLogger("IndexPatterns")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


LOGGER = setup_logger()


def load_progress() -> Dict[str, Any]:
    """Load progress map from JSON file."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.warning("Could not load progress file: %s", exc)
            return {}
    return {}


def save_progress(progress: Dict[str, Any]) -> None:
    """Save progress map to JSON file."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def parse_pattern_metadata(pattern_dir: str) -> Dict[str, str]:
    """Extract metadata (Title, Framework, Description) from pattern directory."""
    folder_name = os.path.basename(pattern_dir)
    readme_path = os.path.join(pattern_dir, "README.md")

    title = folder_name.replace("-", " ").title()
    description = ""

    if os.path.exists(readme_path):
        try:
            with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            lines = [line.strip() for line in content.split("\n") if line.strip()]
            for line in lines:
                if line.startswith("# "):
                    title = line.lstrip("# ").strip()
                    break

            desc_match = re.search(r"## Description\s*\n+([^#]+)", content, re.IGNORECASE)
            if desc_match:
                description = desc_match.group(1).strip()[:400]
            else:
                paragraphs = [p.strip() for p in content.split("\n\n") if p.strip() and not p.startswith("#")]
                if paragraphs:
                    description = paragraphs[0][:400]
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.warning("Parsing README for %s failed: %s", folder_name, exc)

    if "cdk" in folder_name:
        framework = "AWS CDK"
    elif "terraform" in folder_name or "tf" in folder_name:
        framework = "Terraform"
    elif "sam" in folder_name or "sls" in folder_name:
        framework = "AWS SAM / Serverless Framework"
    else:
        framework = "AWS CloudFormation / SAM"

    return {
        "folder": folder_name,
        "title": title,
        "framework": framework,
        "description": description,
    }


def parse_cloudformation_template(
    filepath: str, metadata: Dict[str, str]
) -> List[Document]:
    """Parse CloudFormation / SAM template into atomic Resource, Parameter, and Output blocks using cfn_flip."""
    documents = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        if not content.strip():
            return []

        try:
            data_dict, _ = cfn_flip.load(content)
        except Exception:  # pylint: disable=broad-exception-caught
            # Fallback for non-standard YAML syntax
            doc = Document(
                text=content,
                metadata={
                    "file_path": filepath,
                    "folder": metadata["folder"],
                    "title": metadata["title"],
                    "framework": metadata["framework"],
                    "type": "cloudformation_raw",
                },
            )
            return [doc]

        if not isinstance(data_dict, dict):
            return []

        base_header = (
            f"Pattern Title: {metadata['title']}\n"
            f"Pattern Folder: {metadata['folder']}\n"
            f"Framework: {metadata['framework']}\n"
            f"Summary: {metadata['description']}\n"
        )

        # 1. Extract Parameters & Header
        params = data_dict.get("Parameters", {})
        globals_sec = data_dict.get("Globals", {})
        if params or globals_sec:
            param_yaml = cfn_flip.dump_yaml({"Parameters": params, "Globals": globals_sec})
            doc = Document(
                text=f"{base_header}Section: Template Parameters & Globals\n--- Content ---\n\n{param_yaml}",
                metadata={
                    "file_path": filepath,
                    "folder": metadata["folder"],
                    "title": metadata["title"],
                    "framework": metadata["framework"],
                    "type": "cloudformation_parameters",
                },
            )
            documents.append(doc)

        # 2. Extract Atomic Resources
        resources = data_dict.get("Resources", {})
        if isinstance(resources, dict):
            for res_name, res_body in resources.items():
                if isinstance(res_body, dict):
                    res_type = res_body.get("Type", "UnknownType")
                    res_yaml = cfn_flip.dump_yaml({res_name: res_body})
                    res_header = (
                        f"{base_header}"
                        f"Resource Logical ID: {res_name}\n"
                        f"Resource Type: {res_type}\n"
                        f"--- Content ---\n\n"
                    )
                    doc = Document(
                        text=f"{res_header}{res_yaml}",
                        metadata={
                            "file_path": filepath,
                            "folder": metadata["folder"],
                            "title": metadata["title"],
                            "framework": metadata["framework"],
                            "resource_name": res_name,
                            "resource_type": res_type,
                            "type": "cloudformation_resource",
                        },
                    )
                    documents.append(doc)

        # 3. Extract Outputs
        outputs = data_dict.get("Outputs", {})
        if outputs:
            output_yaml = cfn_flip.dump_yaml({"Outputs": outputs})
            doc = Document(
                text=f"{base_header}Section: Template Outputs\n--- Content ---\n\n{output_yaml}",
                metadata={
                    "file_path": filepath,
                    "folder": metadata["folder"],
                    "title": metadata["title"],
                    "framework": metadata["framework"],
                    "type": "cloudformation_outputs",
                },
            )
            documents.append(doc)

    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.error("Error splitting CloudFormation template %s: %s", filepath, exc)

    return documents


def make_code_splitter_factory():
    """Return a cached factory that builds one CodeSplitter per tree-sitter language.

    CodeSplitter constructs (and loads a grammar) eagerly, so instances are cached
    and reused across pattern directories. Raises a clear ImportError if the optional
    tree-sitter backend is not installed.
    """
    from llama_index.core.node_parser import CodeSplitter  # pylint: disable=import-outside-toplevel

    cache: Dict[str, Any] = {}

    def get(language: str):
        if language not in cache:
            cache[language] = CodeSplitter(language=language)
        return cache[language]

    return get


def split_document(
    doc: Document,
    filepath: str,
    use_code_splitter: bool,
    sentence_splitter: SentenceSplitter,
    code_factory,
) -> List[Any]:
    """Split one document into nodes, using the AST-aware CodeSplitter when applicable.

    Falls back to the SentenceSplitter for unsupported file types and for any file
    the language grammar cannot parse (partial snippets, unusual syntax).
    """
    if use_code_splitter:
        language = CODE_LANG_BY_EXT.get(os.path.splitext(filepath)[1].lower())
        if language:
            try:
                return code_factory(language).get_nodes_from_documents([doc])
            except Exception as exc:  # pylint: disable=broad-exception-caught
                LOGGER.warning(
                    "CodeSplitter (%s) failed for %s; falling back to sentence splitter: %s",
                    language, filepath, exc,
                )
    return sentence_splitter.get_nodes_from_documents([doc])


def index_patterns(
    patterns_dir: str = BASE_PATTERNS_DIR,
    table_suffix: str = TABLE_SUFFIX,
    model_name: str = MODEL_NAME,
    splitter: str = "sentence",
):  # pylint: disable=too-many-statements,too-many-locals,too-many-branches
    """Batch index all pattern directories into PostgreSQL pgvector."""
    LOGGER.info("Starting Serverless Patterns indexing process...")
    LOGGER.info("Initializing Qwen3 embedding model '%s'...", model_name)
    rag_app.setup_settings(model_name)

    vector_store, table_name = rag_app.get_vector_store(model_name, table_suffix)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    patterns_dir = os.path.abspath(patterns_dir)
    pattern_dirs = sorted([
        os.path.join(patterns_dir, d)
        for d in os.listdir(patterns_dir)
        if os.path.isdir(os.path.join(patterns_dir, d)) and not d.startswith(".") and d != "_pattern-model"
    ])

    total_dirs = len(pattern_dirs)
    LOGGER.info("Found %d pattern directories to index into table 'data_%s'.", total_dirs, table_name)

    progress = load_progress()
    sentence_splitter = SentenceSplitter(chunk_size=1024, chunk_overlap=128)

    use_code_splitter = splitter == "code"
    code_factory = None
    if use_code_splitter:
        try:
            code_factory = make_code_splitter_factory()
            code_factory("python")  # Preflight: fail fast if the grammar backend is missing.
        except ImportError:
            LOGGER.error(
                "--splitter code requires the tree-sitter backend. Install it with: "
                "pip install 'tree-sitter-language-pack<1.0'"
            )
            sys.exit(1)
        LOGGER.info(
            "Splitter: code (AST-aware CodeSplitter for %s; sentence-splitter fallback otherwise).",
            ", ".join(sorted(set(CODE_LANG_BY_EXT.values()))),
        )
    else:
        LOGGER.info("Splitter: sentence (SentenceSplitter chunk_size=1024, overlap=128).")

    start_time = time.time()
    indexed_count = 0
    skipped_count = 0
    total_nodes_count = 0

    for idx, pattern_dir in enumerate(pattern_dirs, 1):
        folder_name = os.path.basename(pattern_dir)
        if progress.get(folder_name) == "done":
            skipped_count += 1
            continue

        metadata = parse_pattern_metadata(pattern_dir)
        context_header = (
            f"Pattern Title: {metadata['title']}\n"
            f"Pattern Folder: {metadata['folder']}\n"
            f"Framework: {metadata['framework']}\n"
            f"Summary: {metadata['description']}\n"
            f"--- Content ---\n\n"
        )

        cfn_templates = sorted(
            glob.glob(os.path.join(pattern_dir, "template.yaml"))
            + glob.glob(os.path.join(pattern_dir, "template.yml"))
            + glob.glob(os.path.join(pattern_dir, "template.json"))
            + glob.glob(os.path.join(pattern_dir, "**", "template.yaml"), recursive=True)
            + glob.glob(os.path.join(pattern_dir, "**", "template.yml"), recursive=True)
        )
        cfn_templates = sorted(list(set(cfn_templates)))

        other_file_patterns = [
            "README.md", "*.tf", "*.tfvars", "*.ts", "*.js", "*.mjs",
            "*.py", "*.java", "*.cs", "*.go", "*.graphql", "*.json"
        ]
        other_files = []
        for p in other_file_patterns:
            other_files.extend(glob.glob(os.path.join(pattern_dir, p)))
            other_files.extend(glob.glob(os.path.join(pattern_dir, "**", p), recursive=True))

        junk_keywords = [
            "node_modules", ".venv", "venv", ".git", "dist", "build", ".cdk.out",
            "target", "layers", "layer", ".dist-info", "__pycache__", ".egg-info", ".tox",
            "bin/", "obj/", "package-lock.json", "yarn.lock"
        ]

        # Filter out build artifacts, package locks, binaries, and bundled Lambda layers
        filtered_files = []
        for f in set(other_files) - set(cfn_templates):
            rel_path = f[len(pattern_dir):].lower()
            fname = os.path.basename(f)
            if any(junk in rel_path for junk in junk_keywords):
                continue
            if fname in ["tsconfig.json", "cdk.json", "settings.json"]:
                continue
            if os.path.getsize(f) > 500 * 1024:  # Skip files > 500 KB
                continue
            filtered_files.append(f)

        other_files = sorted(filtered_files)

        if not cfn_templates and not other_files:
            progress[folder_name] = "done"
            save_progress(progress)
            continue

        pattern_nodes = []

        # 1. Process CloudFormation / SAM templates using CloudFormation Resource Splitter.
        # These synthesized YAML+header docs are already atomic, so they always use the
        # sentence splitter (no single code grammar fits, and the header breaks AST parsing).
        for cfn_file in cfn_templates:
            cfn_docs = parse_cloudformation_template(cfn_file, metadata)
            for doc in cfn_docs:
                pattern_nodes.extend(sentence_splitter.get_nodes_from_documents([doc]))

        # 2. Process non-CloudFormation code (CDK TypeScript/Python, Terraform, READMEs).
        for filepath in other_files:
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

                if not content.strip():
                    continue

                # In code mode, code-language files are fed raw to the AST splitter (the
                # prose header would corrupt the parse), so the pattern context is carried
                # in metadata instead. All other files keep the in-text context prefix.
                ext = os.path.splitext(filepath)[1].lower()
                file_uses_code = use_code_splitter and ext in CODE_LANG_BY_EXT
                doc_meta = {
                    "file_path": filepath,
                    "folder": metadata["folder"],
                    "title": metadata["title"],
                    "framework": metadata["framework"],
                    "type": "code_doc",
                }
                if file_uses_code:
                    text = content
                    doc_meta["summary"] = metadata["description"]
                else:
                    text = context_header + content
                doc = Document(text=text, metadata=doc_meta)

                pattern_nodes.extend(
                    split_document(
                        doc, filepath, use_code_splitter, sentence_splitter, code_factory
                    )
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                LOGGER.error("Error reading %s: %s", filepath, exc)

        if pattern_nodes:
            total_nodes_count += len(pattern_nodes)
            VectorStoreIndex(
                nodes=pattern_nodes,
                storage_context=storage_context,
                show_progress=False,
            )
            indexed_count += 1

        progress[folder_name] = "done"

        if idx % 10 == 0 or idx == total_dirs:
            save_progress(progress)
            elapsed = time.time() - start_time
            pct = (idx / total_dirs) * 100
            processed_new = indexed_count
            rate = processed_new / elapsed if elapsed > 0 else 0
            remaining = (total_dirs - idx) / rate if rate > 0 else 0

            LOGGER.info(
                "Progress: [%d/%d] (%.1f%%) | New: %d, Skipped: %d | Nodes: %d | Rate: %.2f pattern/s | Elapsed: %.1fs | ETA: %.1fs",
                idx, total_dirs, pct, indexed_count, skipped_count, total_nodes_count, rate, elapsed, remaining
            )

    save_progress(progress)
    total_elapsed = time.time() - start_time
    LOGGER.info(
        "✅ Indexing Complete! Successfully indexed %d patterns (%d vector nodes) into 'data_%s' in %.1f seconds.",
        indexed_count, total_nodes_count, table_name, total_elapsed
    )


def main():
    """Parse CLI arguments and run patterns indexing."""
    parser = argparse.ArgumentParser(
        description="Index AWS code and serverless patterns into PostgreSQL pgvector."
    )
    parser.add_argument(
        "--patterns-dir",
        default=BASE_PATTERNS_DIR,
        help="Target directory containing code/patterns (default: /home/jmikhail/code/aws/serverless-patterns)",
    )
    parser.add_argument(
        "--table-suffix",
        default=TABLE_SUFFIX,
        help="PostgreSQL table name suffix (default: patterns)",
    )
    parser.add_argument(
        "--model",
        default=MODEL_NAME,
        help="Embedding model name (default: qwen3-0.6b)",
    )
    parser.add_argument(
        "--splitter",
        default="sentence",
        choices=["sentence", "code"],
        help=(
            "Chunking strategy. 'sentence' (default) uses the SentenceSplitter for all files "
            "(original behavior). 'code' uses an AST-aware CodeSplitter per language, falling "
            "back to the sentence splitter for markdown/JSON/CFN docs and unparseable files "
            "(requires: pip install 'tree-sitter-language-pack<1.0')."
        ),
    )
    args = parser.parse_args()

    index_patterns(
        patterns_dir=args.patterns_dir,
        table_suffix=args.table_suffix,
        model_name=args.model,
        splitter=args.splitter,
    )


if __name__ == "__main__":
    main()
