#!/usr/bin/env python3
"""Batch index target AWS documentation directories using rag_app.

- Loads the embedding model ONCE into GPU memory (saving PyTorch reload overhead).
- Tracks progress in .batch_index_progress.json to support resuming if interrupted.
- Validates directories before indexing.
"""

import os
import sys
import time
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rag_app

TARGET_FOLDERS = [
    "AmazonS3", "AWSEC2", "ec2", "lambda", "IAM", "vpc", "AmazonRDS", "rds",
    "AWSCloudFormation", "cloudwatch", "AmazonCloudWatch", "amazondynamodb",
    "eks", "AmazonECS", "Route53", "cloudfront", "AmazonCloudFront", "sns",
    "AWSSimpleQueueService", "apigateway", "kms", "systems-manager",
    "secretsmanager", "ebs", "elasticloadbalancing", "autoscaling",
    "AmazonECR", "efs", "cli", "cdk", "sdk-for-python", "awscloudtrail",
    "organizations", "STS", "AmazonCloudWatchLogs", "awsaccountbilling",
    "cost-management", "aws-cost-management", "cur", "cloudformation-cli",
    "cfn-guard", "infrastructure-composer", "serverless-application-model",
    "cloudcontrolapi", "athena", "sagemaker", "codebuild", "codepipeline",
    "codedeploy", "guardduty", "securityhub", "documentdb", "bedrock",
    "memorydb", "vpn", "serverless"
]

PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".batch_index_progress.json")

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)

def main():
    parser = argparse.ArgumentParser(description="Batch index AWS documentation folders.")
    parser.add_argument("--docs-root", default="../documentation/markdown", help="Root directory containing AWS markdown guides")
    parser.add_argument("--model", default="qwen3-0.6b", help="Embedding model to use")
    parser.add_argument("--clean-progress", action="store_true", help="Reset progress tracking file")
    args = parser.parse_args()

    if args.clean_progress and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("Progress tracker reset.")

    progress = load_progress()
    docs_root = os.path.abspath(args.docs_root)

    print(f"=== BATCH INDEXING {len(TARGET_FOLDERS)} AWS FOLDERS ===")
    print(f"Docs Root: {docs_root}")
    print(f"Embedding Model: {args.model}")
    print("Initializing embedding model into GPU memory once...\n")

    # Load model once
    rag_app.setup_settings(args.model)

    start_total = time.time()
    success_count = 0
    skipped_count = 0

    for idx, folder_name in enumerate(TARGET_FOLDERS, 1):
        target_dir = os.path.join(docs_root, folder_name)

        if folder_name in progress and progress[folder_name].get("completed", False):
            print(f"[{idx}/{len(TARGET_FOLDERS)}] Skipping '{folder_name}' (Already indexed)")
            skipped_count += 1
            continue

        if not os.path.exists(target_dir):
            print(f"[{idx}/{len(TARGET_FOLDERS)}] Warning: Directory missing '{target_dir}' - skipping.")
            continue

        print(f"\n[{idx}/{len(TARGET_FOLDERS)}] Indexing '{folder_name}'...")
        folder_start = time.time()

        try:
            rag_app.cmd_index(
                directory=target_dir,
                clean=False,
                update=False,
                model=args.model,
                table_suffix="",
                reuse_settings=True
            )
            elapsed = time.time() - folder_start
            progress[folder_name] = {
                "completed": True,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_seconds": round(elapsed, 2)
            }
            save_progress(progress)
            success_count += 1
            print(f"Finished '{folder_name}' in {elapsed:.2f} seconds.")
        except Exception as e:
            print(f"Error indexing '{folder_name}': {e}", file=sys.stderr)

    total_time = time.time() - start_total
    print(f"\n==========================================")
    print(f"BATCH INDEXING COMPLETED!")
    print(f"Successfully Indexed: {success_count} folders")
    print(f"Skipped (Already Indexed): {skipped_count} folders")
    print(f"Total Time Elapsed: {total_time/60:.2f} minutes")
    print(f"==========================================")

if __name__ == "__main__":
    main()
