"""Retrieval eval harness for the AWS docs RAG index.

Scores each embedding model on whether it retrieves the documents that
actually contain the answer, using a hand-graded key in questions.json.

Retrieval only: no LLM is called. Generation quality is downstream of
retrieval and costs an API call per question, so measuring the retriever
directly is both cheaper and the thing that actually differs between models.
"""

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llama_index.core import VectorStoreIndex

import rag_app

HERE = os.path.dirname(os.path.abspath(__file__))


def load_questions(path):
    with open(path) as fh:
        return json.load(fh)


def doc_key(file_path):
    """Service-qualified path, e.g. `IAM/latest/UserGuide/id_roles.md`.

    Basenames are not unique across the corpus - `Welcome.md` exists in 7
    services and `CommonErrors.md` in 7 more - so grading on basename alone
    would credit a model for retrieving Lambda's page when the answer was in
    IAM's.
    """
    marker = "/markdown/"
    idx = file_path.find(marker)
    return file_path[idx + len(marker):] if idx >= 0 else os.path.basename(file_path)


def matches(key, gold):
    """A gold entry may be a bare basename or a service-qualified path.

    Bare names stay valid while they are unambiguous; qualify a gold entry
    with its service prefix whenever the basename collides.
    """
    return key == gold or key.endswith("/" + gold)


def score_run(retrieved_files, gold, primary):
    """Metrics for one question. `retrieved_files` is ranked, may repeat.

    An entry in `primary` may be a list, meaning any one of those files
    satisfies the requirement - some answers live either in a standalone doc
    or in the tutorial page that inlines the same steps, and retrieving
    either one genuinely answers the question.
    """
    gold = list(dict.fromkeys(gold))
    groups = [[p] if isinstance(p, str) else list(p) for p in primary]
    # Rank of each file's best-scoring chunk, dedup preserving order.
    ranked = []
    for name in retrieved_files:
        if name not in ranked:
            ranked.append(name)

    hits = [g for g in gold if any(matches(k, g) for k in ranked)]
    # Reciprocal rank of the first gold chunk (chunk-level, not file-level:
    # a gold doc buried at position 5 is worth less than one at position 1).
    rr = 0.0
    for i, key in enumerate(retrieved_files, start=1):
        if any(matches(key, g) for g in gold):
            rr = 1.0 / i
            break

    satisfied = sum(1 for grp in groups
                    if any(matches(k, f) for f in grp for k in ranked))

    return {
        "recall": len(hits) / len(gold) if gold else 0.0,
        "primary_recall": satisfied / len(groups) if groups else 0.0,
        "hit": 1.0 if hits else 0.0,
        "mrr": rr,
        # Precision counts chunks, not files: 5 chunks from one gold doc is
        # a less useful context window than 5 chunks from 3 gold docs.
        "precision": sum(1 for k in retrieved_files
                         if any(matches(k, g) for g in gold)) / len(retrieved_files),
        "distinct_files": len(ranked),
        "found": sorted(hits),
        "missed": sorted(set(gold) - set(hits)),
        "retrieved": retrieved_files,
    }


def eval_model(model, questions, top_k):
    rag_app.setup_settings(model, is_query=False)
    vector_store, _ = rag_app.get_vector_store(model)
    index = VectorStoreIndex.from_vector_store(vector_store=vector_store)
    retriever = index.as_retriever(similarity_top_k=top_k * 4)
    dedup_processor = rag_app.FileDeduplicationPostprocessor(max_chunks_per_file=1)

    results = []
    for q in questions:
        start = time.time()
        candidate_nodes = retriever.retrieve(q["question"])
        nodes = dedup_processor.postprocess_nodes(candidate_nodes)[:top_k]
        latency = time.time() - start

        files = [doc_key(n.node.metadata.get("file_path", "?")) for n in nodes]
        scores = [n.score for n in nodes if n.score is not None]

        row = score_run(files, q["gold_files"], q["primary_files"])
        row.update(
            id=q["id"],
            difficulty=q["difficulty"],
            latency=latency,
            top_score=max(scores) if scores else 0.0,
            # A flat score band means nothing matched strongly and the
            # retriever returned whatever was nearest.
            score_spread=(max(scores) - min(scores)) if len(scores) > 1 else 0.0,
        )
        results.append(row)
        print(f"  {q['id']:<24} recall {row['recall']:.2f}  "
              f"primary {row['primary_recall']:.2f}  mrr {row['mrr']:.2f}",
              flush=True)
    return results


def mean(rows, key):
    return statistics.fmean(r[key] for r in rows) if rows else 0.0


def summarize(by_model, questions):
    difficulties = []
    for q in questions:
        if q["difficulty"] not in difficulties:
            difficulties.append(q["difficulty"])

    print("\n" + "=" * 78)
    print("OVERALL")
    print("=" * 78)
    head = f"{'model':<14}{'recall':>8}{'primary':>9}{'hit':>7}{'MRR':>7}{'prec':>7}{'files':>7}{'spread':>8}{'sec':>7}"
    print(head)
    print("-" * len(head))
    for model, rows in by_model.items():
        print(f"{model:<14}{mean(rows,'recall'):>8.3f}{mean(rows,'primary_recall'):>9.3f}"
              f"{mean(rows,'hit'):>7.3f}{mean(rows,'mrr'):>7.3f}{mean(rows,'precision'):>7.3f}"
              f"{mean(rows,'distinct_files'):>7.2f}{mean(rows,'score_spread'):>8.3f}"
              f"{mean(rows,'latency'):>7.2f}")

    for diff in difficulties:
        print(f"\n--- {diff} (primary_recall) ---")
        for model, rows in by_model.items():
            sub = [r for r in rows if r["difficulty"] == diff]
            print(f"{model:<14}{mean(sub,'primary_recall'):>8.3f}")

    print("\n--- per-question primary_recall ---")
    models = list(by_model)
    print(f"{'question':<24}" + "".join(f"{m:>13}" for m in models))
    for q in questions:
        line = f"{q['id']:<24}"
        for m in models:
            row = next(r for r in by_model[m] if r["id"] == q["id"])
            line += f"{row['primary_recall']:>13.2f}"
        print(line)

    print("\n--- misses (primary docs never retrieved) ---")
    for model, rows in by_model.items():
        misses = [r["id"] for r in rows if r["primary_recall"] < 1.0]
        print(f"{model:<14}{', '.join(misses) if misses else '(none)'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="+",
                    default=["qwen3-0.6b"])
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "results.json"))
    ap.add_argument("--only", nargs="+", help="Run just these question ids")
    args = ap.parse_args()

    spec = load_questions(args.questions)
    questions = spec["questions"]
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
        if not questions:
            sys.exit("No questions matched --only")

    by_model = {}
    for model in args.models:
        print(f"\n=== {model} (top_k={args.top_k}) ===", flush=True)
        try:
            by_model[model] = eval_model(model, questions, args.top_k)
        except Exception as exc:
            # A model with no index yet shouldn't abort the whole sweep.
            print(f"  SKIPPED: {type(exc).__name__}: {exc}", file=sys.stderr)

    if not by_model:
        sys.exit("No model produced results.")

    summarize(by_model, questions)

    with open(args.out, "w") as fh:
        json.dump({"top_k": args.top_k, "results": by_model}, fh, indent=2)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
