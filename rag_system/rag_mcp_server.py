"""MCP (Model Context Protocol) Server for AWS Documentation RAG System.

Exposes tools for semantic retrieval and LLM answer synthesis over stdio and SSE/HTTP
transports for integration with Claude Desktop, Gemini CLI/API, Antigravity, and web agents.
"""

import argparse
import os
import time

from mcp.server.mcpserver import MCPServer

app = MCPServer(name="aws-docs-rag")
DEFAULT_LIMIT = 5


@app.tool()
def query_aws_docs(query: str, limit: int = 5, llm: str = "gemini") -> str:
    """Query the AWS Documentation RAG system to generate an authoritative answer.

    Args:
        query: Technical question about AWS services (e.g. S3, EC2, IAM, Lambda, VPC, RDS).
        limit: Number of context documents to retrieve (default: 5).
        llm: Synthesis model choice, either 'gemini' (gemini-2.5-flash) or 'claude' (claude-3-5-sonnet).

    Returns:
        Formatted answer string including retrieved ground-truth sources and latency.
    """
    try:
        from llama_index.core import VectorStoreIndex  # pylint: disable=import-outside-toplevel
        import rag_app  # pylint: disable=import-outside-toplevel

        effective_limit = limit if limit is not None else DEFAULT_LIMIT
        rag_app.setup_settings("qwen3-0.6b", is_query=True, llm_choice=llm)
        vector_store, _ = rag_app.get_vector_store("qwen3-0.6b")
        index = VectorStoreIndex.from_vector_store(vector_store=vector_store)

        candidate_k = effective_limit * 4
        dedup_processor = rag_app.FileDeduplicationPostprocessor(max_chunks_per_file=1)
        window_processor = rag_app.MetadataReplacementPostProcessor(target_metadata_key="window")
        query_engine = index.as_query_engine(
            similarity_top_k=candidate_k,
            node_postprocessors=[dedup_processor, window_processor],
        )

        start_time = time.time()
        response = query_engine.query(query)
        latency = time.time() - start_time

        output_lines = [
            f"### Answer\n{response}\n",
            "### Ground-Truth Sources",
        ]
        for source in response.source_nodes:
            metadata = source.node.metadata
            file_path = metadata.get("file_path", "Unknown")
            score = source.score if source.score is not None else 0.0
            marker = "/markdown/"
            idx = file_path.find(marker)
            label = (
                file_path[idx + len(marker) :]
                if idx >= 0
                else os.path.basename(file_path)
            )
            output_lines.append(f"- **{label}** (Similarity: {score:.4f})")

        output_lines.append(f"\n*Query Latency: {latency:.2f} seconds*")
        return "\n".join(output_lines)

    except (Exception, SystemExit) as exc:  # pylint: disable=broad-exception-caught
        return f"Error executing RAG query: {type(exc).__name__}: {exc}"


@app.tool()
def search_aws_docs(query: str, limit: int = 5) -> str:
    """Perform semantic vector retrieval across AWS documentation without LLM synthesis.

    Args:
        query: Search query or concept to look up.
        limit: Number of matching passages to return (default: 5).

    Returns:
        Formatted list of retrieved document passages, scores, and file paths.
    """
    try:
        from llama_index.core import VectorStoreIndex  # pylint: disable=import-outside-toplevel
        import rag_app  # pylint: disable=import-outside-toplevel

        effective_limit = limit if limit is not None else DEFAULT_LIMIT
        rag_app.setup_settings("qwen3-0.6b", is_query=False)
        vector_store, _ = rag_app.get_vector_store("qwen3-0.6b")
        index = VectorStoreIndex.from_vector_store(vector_store=vector_store)

        retriever = index.as_retriever(similarity_top_k=effective_limit * 4)
        dedup_processor = rag_app.FileDeduplicationPostprocessor(max_chunks_per_file=1)
        raw_nodes = retriever.retrieve(query)
        nodes = dedup_processor.postprocess_nodes(raw_nodes)[:effective_limit]

        output_lines = [f"### Vector Search Results for: '{query}'\n"]
        for i, n in enumerate(nodes, 1):
            metadata = n.node.metadata
            file_path = metadata.get("file_path", "Unknown")
            score = n.score if n.score is not None else 0.0
            marker = "/markdown/"
            idx = file_path.find(marker)
            label = (
                file_path[idx + len(marker) :]
                if idx >= 0
                else os.path.basename(file_path)
            )
            text_preview = n.node.get_content().strip()
            if len(text_preview) > 400:
                text_preview = text_preview[:400] + "..."
            output_lines.append(f"#### {i}. {label} (Score: {score:.4f})")
            output_lines.append(f"```text\n{text_preview}\n```\n")

        return "\n".join(output_lines)

    except (Exception, SystemExit) as exc:  # pylint: disable=broad-exception-caught
        return f"Error executing vector search: {type(exc).__name__}: {exc}"


@app.tool()
def list_indexed_services() -> str:
    """List the AWS services currently indexed in the RAG database."""
    services = [
        "AmazonS3 (Simple Storage Service)",
        "AWSEC2 (Elastic Compute Cloud)",
        "IAM (Identity and Access Management)",
        "lambda (AWS Lambda)",
        "vpc (Virtual Private Cloud)",
        "AmazonRDS (Relational Database Service)",
        "AWSCloudFormation (Infrastructure as Code)",
        "AmazonCloudWatch / cloudwatch (Monitoring & Observability)",
    ]
    return "### Indexed AWS Documentation Services\n\n" + "\n".join(
        f"- {s}" for s in services
    )


def main():
    """Parse transport arguments and launch the MCP server."""
    parser = argparse.ArgumentParser(
        description="MCP Server for AWS Documentation RAG System"
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
        print(f"Starting AWS Docs RAG MCP Server on {args.host}:{args.port} ({args.transport}) [default limit={DEFAULT_LIMIT}]...")
        app.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
