"""MCP (Model Context Protocol) Server for AWS Documentation & Serverless Patterns RAG System.

Exposes tools for semantic retrieval and LLM answer synthesis over stdio and SSE/HTTP
transports for integration with Claude Desktop, Gemini CLI/API, Antigravity, and web agents.
"""

import argparse
import os
import time
from typing import List

from mcp.server.mcpserver import MCPServer

app = MCPServer(name="aws-docs-rag")
DEFAULT_LIMIT = 5


def format_source_label(file_path: str) -> str:
    """Format file path metadata into a readable ground-truth source label."""
    patterns_marker = "/serverless-patterns/"
    markdown_marker = "/markdown/"
    if patterns_marker in file_path:
        idx = file_path.find(patterns_marker)
        return "[Serverless Pattern] " + file_path[idx + len(patterns_marker) :]
    elif markdown_marker in file_path:
        idx = file_path.find(markdown_marker)
        return "[AWS Doc] " + file_path[idx + len(markdown_marker) :]
    else:
        return os.path.basename(file_path)


def retrieve_nodes(query: str, limit: int, corpus: str = "all"):
    """Retrieve and deduplicate vector nodes across standard AWS docs, serverless patterns, or both."""
    from llama_index.core import VectorStoreIndex  # pylint: disable=import-outside-toplevel
    import rag_app  # pylint: disable=import-outside-toplevel

    effective_limit = limit if limit is not None else DEFAULT_LIMIT
    candidate_k = effective_limit * 4

    nodes = []
    if corpus in ("docs", "all"):
        vstore_docs, _ = rag_app.get_vector_store("qwen3-0.6b", table_suffix="")
        idx_docs = VectorStoreIndex.from_vector_store(vstore_docs)
        r_docs = idx_docs.as_retriever(similarity_top_k=candidate_k)
        nodes.extend(r_docs.retrieve(query))

    if corpus in ("patterns", "all"):
        vstore_pats, _ = rag_app.get_vector_store("qwen3-0.6b", table_suffix="patterns")
        idx_pats = VectorStoreIndex.from_vector_store(vstore_pats)
        r_pats = idx_pats.as_retriever(similarity_top_k=candidate_k)
        nodes.extend(r_pats.retrieve(query))

    sorted_nodes = sorted(
        nodes, key=lambda n: n.score if n.score is not None else 0.0, reverse=True
    )
    dedup_processor = rag_app.FileDeduplicationPostprocessor(max_chunks_per_file=1)
    window_processor = rag_app.MetadataReplacementPostProcessor(target_metadata_key="window")

    processed_nodes = window_processor.postprocess_nodes(
        dedup_processor.postprocess_nodes(sorted_nodes)
    )[:effective_limit]
    return processed_nodes


@app.tool()
def query_aws_docs(
    query: str, limit: int = 5, llm: str = "gemini", corpus: str = "all"
) -> str:
    """Query the AWS RAG system (documentation & serverless code patterns) to generate an authoritative answer.

    Args:
        query: Technical question about AWS services or code patterns (e.g. S3, EC2, IAM, Lambda, VPC, SAM, CDK).
        limit: Number of context documents to retrieve (default: 5).
        llm: Synthesis model choice, either 'gemini' (gemini-2.5-flash) or 'claude' (claude-3-5-sonnet).
        corpus: Knowledge base to query: 'all' (docs + serverless patterns), 'docs' (AWS documentation only), or 'patterns' (Serverless Land code patterns only).

    Returns:
        Formatted answer string including retrieved ground-truth sources and latency.
    """
    try:
        from llama_index.core.response_synthesizers import get_response_synthesizer  # pylint: disable=import-outside-toplevel
        import rag_app  # pylint: disable=import-outside-toplevel

        effective_limit = limit if limit is not None else DEFAULT_LIMIT
        rag_app.setup_settings("qwen3-0.6b", is_query=True, llm_choice=llm)

        start_time = time.time()
        nodes = retrieve_nodes(query, effective_limit, corpus=corpus)
        synthesizer = get_response_synthesizer()
        response = synthesizer.synthesize(query, nodes=nodes)
        latency = time.time() - start_time

        output_lines = [
            f"### Answer\n{response}\n",
            "### Ground-Truth Sources",
        ]
        for source in response.source_nodes:
            metadata = source.node.metadata
            file_path = metadata.get("file_path", "Unknown")
            score = source.score if source.score is not None else 0.0
            label = format_source_label(file_path)
            output_lines.append(f"- **{label}** (Similarity: {score:.4f})")

        output_lines.append(f"\n*Query Latency: {latency:.2f} seconds*")
        return "\n".join(output_lines)

    except (Exception, SystemExit) as exc:  # pylint: disable=broad-exception-caught
        return f"Error executing RAG query: {type(exc).__name__}: {exc}"


@app.tool()
def query_serverless_patterns(
    query: str, limit: int = 5, llm: str = "gemini"
) -> str:
    """Query 7,000+ AWS Serverless Land code patterns (SAM, CDK, Terraform, Lambda code handlers).

    Args:
        query: Technical question or request for serverless architecture patterns/code (e.g. "EventBridge to Lambda in SAM", "S3 to SQS in CDK").
        limit: Number of context documents to retrieve (default: 5).
        llm: Synthesis model choice, either 'gemini' (gemini-2.5-flash) or 'claude' (claude-3-5-sonnet).

    Returns:
        Formatted answer string with code examples, Ground-Truth sources, and latency.
    """
    return query_aws_docs(query=query, limit=limit, llm=llm, corpus="patterns")


@app.tool()
def search_aws_docs(query: str, limit: int = 5, corpus: str = "all") -> str:
    """Perform semantic vector retrieval across AWS documentation and serverless patterns without LLM synthesis.

    Args:
        query: Search query or concept to look up.
        limit: Number of matching passages to return (default: 5).
        corpus: Knowledge base to search: 'all' (docs + serverless patterns), 'docs' (AWS documentation only), or 'patterns' (Serverless Land code patterns only).

    Returns:
        Formatted list of retrieved document passages, scores, and file paths.
    """
    try:
        import rag_app  # pylint: disable=import-outside-toplevel

        effective_limit = limit if limit is not None else DEFAULT_LIMIT
        rag_app.setup_settings("qwen3-0.6b", is_query=False)

        nodes = retrieve_nodes(query, effective_limit, corpus=corpus)

        output_lines = [f"### Vector Search Results for: '{query}' (Corpus: {corpus})\n"]
        for i, n in enumerate(nodes, 1):
            metadata = n.node.metadata
            file_path = metadata.get("file_path", "Unknown")
            score = n.score if n.score is not None else 0.0
            label = format_source_label(file_path)
            text_preview = n.node.get_content().strip()
            if len(text_preview) > 500:
                text_preview = text_preview[:500] + "..."
            output_lines.append(f"#### {i}. {label} (Score: {score:.4f})")
            output_lines.append(f"```text\n{text_preview}\n```\n")

        return "\n".join(output_lines)

    except (Exception, SystemExit) as exc:  # pylint: disable=broad-exception-caught
        return f"Error executing vector search: {type(exc).__name__}: {exc}"


@app.tool()
def search_serverless_patterns(query: str, limit: int = 5) -> str:
    """Perform semantic vector retrieval over AWS Serverless Land code patterns without LLM synthesis.

    Args:
        query: Search query for serverless code snippets, SAM templates, or CDK constructs.
        limit: Number of matching passages to return (default: 5).

    Returns:
        Formatted list of retrieved pattern code snippets, scores, and file paths.
    """
    return search_aws_docs(query=query, limit=limit, corpus="patterns")


@app.tool()
def list_indexed_services() -> str:
    """List the AWS services and code pattern repositories currently indexed in the RAG database."""
    doc_services = [
        "AmazonS3 (Simple Storage Service)",
        "AWSEC2 (Elastic Compute Cloud)",
        "IAM (Identity and Access Management)",
        "lambda (AWS Lambda)",
        "vpc (Virtual Private Cloud)",
        "AmazonRDS (Relational Database Service)",
        "AWSCloudFormation (Infrastructure as Code)",
        "AmazonCloudWatch / cloudwatch (Monitoring & Observability)",
        "AmazonSageMaker (Machine Learning)",
        "AmazonCloudFront (CDN)",
        "AmazonDynamoDB (NoSQL Database)",
    ]
    pattern_summary = [
        "AWS Serverless Land Patterns (7,072 files / 14,405 vector nodes)",
        "  - Infrastructure as Code: AWS SAM, AWS CDK (TypeScript/Python/Java), Terraform",
        "  - Runtimes & Handlers: Python, Node.js/TypeScript, C# (.NET), Java, Go",
        "  - Event Sources & Integrations: EventBridge, SQS, SNS, DynamoDB Streams, API Gateway, AppSync, Step Functions, S3",
    ]
    lines = [
        "### Indexed Knowledge Base Corpora\n",
        "#### 1. Official AWS Documentation (Table: data_aws_docs_qwen3_0_6b)",
        "\n".join(f"- {s}" for s in doc_services),
        "\n#### 2. Serverless Land Code Patterns (Table: data_aws_docs_qwen3_0_6bpatterns)",
        "\n".join(f"- {p}" for p in pattern_summary),
    ]
    return "\n".join(lines)


def main():
    """Parse transport arguments and launch the MCP server."""
    parser = argparse.ArgumentParser(
        description="MCP Server for AWS Documentation & Serverless Patterns RAG System"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport mechanism (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host interface for SSE/HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port number for SSE/HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--default-limit",
        type=int,
        default=5,
        help="Default number of passages to retrieve for queries (default: 5)",
    )

    args = parser.parse_args()

    global DEFAULT_LIMIT  # pylint: disable=global-statement
    DEFAULT_LIMIT = args.default_limit

    if args.transport == "stdio":
        app.run(transport="stdio")
    else:
        print(
            f"Starting AWS Docs & Patterns RAG MCP Server on {args.host}:{args.port} ({args.transport}) [default limit={DEFAULT_LIMIT}]..."
        )
        app.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
