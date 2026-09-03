"""Compare generation answers for AWS questions: WITH RAG (retrieved context) vs WITHOUT RAG (LLM alone)."""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llama_index.core import VectorStoreIndex
import rag_app

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen3-0.6b", help="Embedding model to use for retrieval")
    ap.add_argument("--llm", default="gemini", choices=["gemini", "claude"], help="LLM for answer generation")
    ap.add_argument("--limit", type=int, default=5, help="Number of context documents for RAG")
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.json"), help="Path to questions JSON file")
    ap.add_argument("--out", default=os.path.join(HERE, "rag_vs_llm_comparison.md"), help="Path to output markdown report")
    ap.add_argument("--only", nargs="+", help="Run specific question IDs (e.g. --only static-website mrap-failover)")
    args = ap.parse_args()

    with open(args.questions) as fh:
        spec = json.load(fh)
    questions = spec["questions"]

    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
        if not questions:
            sys.exit("No questions matched --only")

    print(f"=== Comparing RAG vs LLM Alone ({len(questions)} questions) ===")
    print(f"Embedding Model: {args.model} | Synthesis LLM: {args.llm}\n", flush=True)

    rag_app.setup_settings(args.model, is_query=True, llm_choice=args.llm)
    vector_store, _ = rag_app.get_vector_store(args.model)
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store)

    candidate_k = args.limit * 4
    dedup_processor = rag_app.FileDeduplicationPostprocessor(max_chunks_per_file=1)
    query_engine = index.as_query_engine(
        similarity_top_k=candidate_k,
        node_postprocessors=[dedup_processor]
    )

    comparison_results = []

    for idx, q in enumerate(questions, 1):
        q_id = q["id"]
        q_text = q["question"]
        diff = q["difficulty"]

        print(f"[{idx}/{len(questions)}] Processing '{q_id}' ({diff})...", flush=True)

        # 1. RAG Answer (With Retrieved AWS Documentation)
        t0 = time.time()
        rag_resp = query_engine.query(q_text)
        rag_latency = time.time() - t0
        rag_text = str(rag_resp).strip()

        sources = []
        for src in rag_resp.source_nodes:
            fp = src.node.metadata.get("file_path", "?")
            marker = "/markdown/"
            pos = fp.find(marker)
            label = fp[pos + len(marker):] if pos >= 0 else os.path.basename(fp)
            score = src.score if src.score is not None else 0.0
            sources.append(f"{label} (score: {score:.4f})")

        # 2. No-RAG LLM Alone Answer (Zero-Shot / No Context)
        prompt = f"Answer the following question about AWS accurately and concisely:\n\n{q_text}"
        t1 = time.time()
        no_rag_resp = rag_app.Settings.llm.complete(prompt)
        no_rag_latency = time.time() - t1
        no_rag_text = str(no_rag_resp).strip()

        comparison_results.append({
            "id": q_id,
            "difficulty": diff,
            "question": q_text,
            "rag_answer": rag_text,
            "rag_latency": rag_latency,
            "sources": sources,
            "no_rag_answer": no_rag_text,
            "no_rag_latency": no_rag_latency
        })

    # Generate Markdown Report
    report_lines = []
    report_lines.append("# RAG vs. LLM Alone Comparison Report\n")
    report_lines.append(f"**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"**Embedding Model:** `{args.model}`")
    report_lines.append(f"**Synthesis LLM:** `{args.llm}`")
    report_lines.append(f"**Total Questions Evaluated:** {len(questions)}\n")

    report_lines.append("## Summary Table\n")
    report_lines.append("| Question ID | Difficulty | RAG Latency | LLM Alone Latency | Top Source File |")
    report_lines.append("| :--- | :--- | :--- | :--- | :--- |")
    for r in comparison_results:
        top_src = r["sources"][0].split(" ")[0] if r["sources"] else "None"
        report_lines.append(f"| `{r['id']}` | {r['difficulty'].upper()} | {r['rag_latency']:.2f}s | {r['no_rag_latency']:.2f}s | `{top_src}` |")

    report_lines.append("\n---\n")
    report_lines.append("## Detailed Answer Side-by-Side Comparisons\n")

    for r in comparison_results:
        report_lines.append(f"### Question: {r['question']} (`{r['id']}` - {r['difficulty'].upper()})\n")
        report_lines.append("#### 🟢 WITH RAG (Retrieved AWS Documentation Context)\n")
        report_lines.append(r['rag_answer'])
        report_lines.append("\n**Retrieved Sources:**")
        for s in r['sources']:
            report_lines.append(f"- `{s}`")
        report_lines.append(f"\n*RAG Latency:* {r['rag_latency']:.2f}s\n")

        report_lines.append("#### 🔵 WITHOUT RAG (LLM Alone / Zero-Shot)\n")
        report_lines.append(r['no_rag_answer'])
        report_lines.append(f"\n*LLM Alone Latency:* {r['no_rag_latency']:.2f}s\n")
        report_lines.append("---\n")

    with open(args.out, "w") as fh:
        fh.write("\n".join(report_lines))

    print(f"\nCompleted! Comparison report written to '{args.out}'")


if __name__ == "__main__":
    main()
