"""Index the full multi-service AWS doc corpus across every embedding model.

All services go into one table per model. Cross-service questions ("how do I
securely give an EC2 instance access to an S3 bucket?") need IAM, S3, EC2 and
VPC docs reachable in a single retrieval, so splitting services across tables
would make those questions unanswerable by construction.

The embedding model is loaded once per model rather than once per directory,
and progress is checkpointed after each directory, so an interrupted run
resumes where it stopped instead of re-embedding hours of work.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

import rag_app

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".index_state.json")

# Service doc sets. AmazonS3 is included so the existing S3 eval questions
# still run against this index - with 6x more corpus as distractors, which
# makes them a harder and more honest test than S3 alone.
#
# The lowercase names are not duplicates of the
# CamelCase ones: AWSEC2 is the EC2 User Guide + API Reference while ec2 is
# the Developer Guide / instance types / Windows AMI reference, and
# AmazonCloudWatch is CloudWatch itself while cloudwatch is Application
# Insights + Observability Admin. Measured filename overlap is under 10.
SERVICES = [
    "AmazonS3",
    "AWSEC2", "ec2", "lambda", "IAM", "vpc",
    "AmazonRDS", "rds", "AWSCloudFormation", "cloudwatch", "AmazonCloudWatch",
]

# Guard against the failure mode of pointing this at the whole documentation
# tree (125k files, ~8 hours per model) by accident.
MAX_FILES = 25000


def count_md(path):
    return sum(1 for root, _, files in os.walk(path)
               for f in files if f.endswith(".md"))


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as fh:
            return json.load(fh)
    return {}


def save_state(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=2)


def drop_table(model, suffix):
    table = "data_" + rag_app.MODELS[model]["table"] + suffix
    conn = psycopg2.connect(dbname=rag_app.DB_NAME, user=rag_app.DB_USER,
                            password=rag_app.DB_PASSWORD, host=rag_app.DB_HOST,
                            port=rag_app.DB_PORT)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    conn.close()
    print(f"  dropped {table}")


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="+",
                    default=["qwen3-0.6b"])
    ap.add_argument("--services", nargs="+", default=SERVICES)
    ap.add_argument("--docs-root", default="../documentation/markdown")
    ap.add_argument("--suffix", default="",
                    help="Optional table suffix, to build a second corpus "
                         "alongside the main one without replacing it")
    ap.add_argument("--resume", action="store_true",
                    help="Skip directories already recorded in the checkpoint")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help=f"Proceed even past the {MAX_FILES}-file guard")
    args = ap.parse_args()

    paths = []
    for name in args.services:
        path = os.path.join(args.docs_root, name)
        if not os.path.isdir(path):
            sys.exit(f"Missing directory: {path}")
        paths.append((name, path, count_md(path)))

    total_files = sum(n for _, _, n in paths)
    print(f"{len(paths)} directories, {total_files} markdown files")
    for name, _, n in paths:
        print(f"  {name:<22}{n:>7}")

    if total_files > MAX_FILES and not args.force:
        sys.exit(f"\nRefusing: {total_files} files exceeds the {MAX_FILES} guard. "
                 f"Pass --force if this is really what you want.")
    # The corpus chunks to roughly 2.55 nodes per file (S3: 1826 files ->
    # 4656 nodes) and embeds at ~11 nodes/sec on the RTX 2000 Ada. Rough:
    # bge-m3 and nomic are smaller models and run at least as fast.
    est = total_files * 2.55 / 11.0 * len(args.models)
    print(f"\nModels: {', '.join(args.models)}   suffix: '{args.suffix}'")
    print(f"Rough estimate: {fmt(est)} total\n")

    if args.dry_run:
        print("Dry run - nothing indexed.")
        return

    state = load_state() if args.resume else {}
    run_start = time.time()

    for model in args.models:
        done = set(state.get(model, []))
        pending = [(n, p, c) for n, p, c in paths if n not in done]
        if not pending:
            print(f"=== {model}: already complete, skipping ===")
            continue

        print(f"\n{'=' * 70}\n=== {model} ({len(pending)} directories to go)\n{'=' * 70}")
        # Loading the model is the expensive part; do it once, then index
        # every directory with reuse_settings so it is not reloaded.
        rag_app.setup_settings(model)

        if not done:
            drop_table(model, args.suffix)

        for name, path, n_files in pending:
            print(f"\n--- {model} / {name} ({n_files} files) ---", flush=True)
            start = time.time()
            try:
                rag_app.cmd_index(path, clean=False, model=model,
                                  table_suffix=args.suffix, reuse_settings=True)
            except Exception as exc:
                # Keep the checkpoint honest: a failed directory is not
                # recorded, so --resume retries exactly that one.
                print(f"FAILED {model}/{name}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                save_state(state)
                continue
            elapsed = time.time() - start
            done.add(name)
            state[model] = sorted(done)
            save_state(state)
            print(f"--- {name} done in {fmt(elapsed)} "
                  f"(elapsed {fmt(time.time() - run_start)}) ---", flush=True)

    print(f"\nAll done in {fmt(time.time() - run_start)}. Checkpoint: {STATE}")
    tables = ", ".join(f"data_{rag_app.MODELS[m]['table']}{args.suffix}"
                       for m in args.models)
    print(f"Tables: {tables}")


if __name__ == "__main__":
    main()
