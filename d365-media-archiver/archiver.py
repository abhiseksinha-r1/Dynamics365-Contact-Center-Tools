#!/usr/bin/env python3
"""
archiver.py — D365 Contact Center Media Archiver
================================================
Exports audio recordings, screen recordings, and transcripts from
Dynamics 365 Contact Center (Dataverse) to Azure Blob Storage, then
optionally removes or retains the source records.

Usage:
  python archiver.py --config config.yaml
  python archiver.py --config config.yaml --pipeline audio
  python archiver.py --config config.yaml --dry-run
  python archiver.py --config config.yaml --discover-schema
  python archiver.py --config config.yaml --print-lifecycle-policy
  python archiver.py --config config.yaml --manifest-report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import config as cfg_module
from blob_client import BlobClient
from dataverse_client import DataverseClient
from manifest import Manifest
import lifecycle_policy


# ------------------------------------------------------------------ #
# CLI                                                                  #
# ------------------------------------------------------------------ #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="D365 Contact Center Media Archiver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument(
        "--pipeline",
        choices=["audio", "screen", "transcript", "all"],
        default="all",
        help="Which pipeline to run (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and log what would be exported, without downloading or deleting anything.",
    )
    parser.add_argument(
        "--discover-schema",
        action="store_true",
        help="Discover file attribute names for all relevant entities and exit.",
    )
    parser.add_argument(
        "--print-lifecycle-policy",
        action="store_true",
        help="Print the Azure Blob lifecycle policy JSON and exit.",
    )
    parser.add_argument(
        "--apply-lifecycle-policy",
        action="store_true",
        help="Apply the lifecycle policy to the Azure storage account (requires --subscription, --resource-group, --storage-account).",
    )
    parser.add_argument("--subscription", help="Azure subscription ID (for --apply-lifecycle-policy)")
    parser.add_argument("--resource-group", help="Azure resource group (for --apply-lifecycle-policy)")
    parser.add_argument("--storage-account", help="Azure storage account name (for --apply-lifecycle-policy)")
    parser.add_argument(
        "--manifest-report",
        action="store_true",
        help="Print a summary of the manifest file and exit.",
    )
    return parser


# ------------------------------------------------------------------ #
# Logging                                                              #
# ------------------------------------------------------------------ #

def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ------------------------------------------------------------------ #
# Schema discovery                                                     #
# ------------------------------------------------------------------ #

def discover_schema(dv: DataverseClient) -> None:
    entities = {
        "msdyn_ocrecording":    "Audio Recordings",
        "msdyn_screenrecording": "Screen Recordings",
        "msdyn_transcript":     "Transcripts (metadata)",
    }
    print("\n=== File Attribute Discovery ===\n")
    for logical_name, label in entities.items():
        print(f"  {label} ({logical_name}):")
        try:
            attrs = dv.discover_file_attributes(logical_name)
            if attrs:
                for a in attrs:
                    print(f"    ✓ {a.get('LogicalName')}  ({a.get('DisplayName', {}).get('UserLocalizedLabel', {}).get('Label', '')})")
            else:
                print("    (no File-type attributes found — entity may use annotation or mediauri)")
        except Exception as exc:
            print(f"    ERROR: {exc}")
    print()
    print("Update config.yaml:")
    print("  audio.file_attribute_name: msdyn_recording   (default, usually correct)")
    print("  screen.file_attribute_name: <discovered above>")
    print()


# ------------------------------------------------------------------ #
# Manifest report                                                      #
# ------------------------------------------------------------------ #

def manifest_report(manifest: Manifest) -> None:
    summary = manifest.summary()
    print("\n=== Manifest Summary ===\n")
    if not summary:
        print("  No records in manifest yet.")
        return
    total_bytes = 0
    for pipeline, counts in summary.items():
        tb = counts.get("total_bytes", 0)
        total_bytes += tb
        print(f"  Pipeline: {pipeline}")
        print(f"    archived : {counts.get('archived', 0)}")
        print(f"    failed   : {counts.get('failed', 0)}")
        print(f"    skipped  : {counts.get('skipped', 0)}")
        print(f"    total GB : {tb / (1024**3):.3f}")
        print()
    print(f"  Grand total: {total_bytes / (1024**3):.3f} GB archived")
    print()


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Load config
    try:
        cfg = cfg_module.load(args.config)
    except cfg_module.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # CLI --dry-run overrides config
    if args.dry_run:
        cfg["run"]["dry_run"] = True

    setup_logging(cfg["run"]["log_level"])
    logger = logging.getLogger("archiver")

    dry_run = cfg["run"]["dry_run"]
    if dry_run:
        logger.info("*** DRY-RUN MODE — no downloads or deletions will occur ***")

    # -- Print lifecycle policy --
    if args.print_lifecycle_policy:
        lifecycle_policy.print_policy(cfg)
        return 0

    # -- Apply lifecycle policy --
    if args.apply_lifecycle_policy:
        if not all([args.subscription, args.resource_group, args.storage_account]):
            print("ERROR: --apply-lifecycle-policy requires --subscription, --resource-group, --storage-account", file=sys.stderr)
            return 1
        lifecycle_policy.apply_policy(cfg, args.subscription, args.resource_group, args.storage_account)
        return 0

    # -- Build shared clients --
    try:
        dv = DataverseClient(cfg)
        blob = BlobClient(cfg)
    except Exception as exc:
        logger.error("Client initialization failed: %s", exc)
        return 1

    # -- Schema discovery --
    if args.discover_schema:
        discover_schema(dv)
        return 0

    # Ensure Azure Blob containers exist
    if not dry_run:
        try:
            blob.ensure_containers_exist()
        except Exception as exc:
            logger.warning("Could not ensure blob containers: %s", exc)

    # -- Load manifest --
    manifest = Manifest(
        path=cfg["manifest"]["path"],
        append_mode=cfg["manifest"]["append_mode"],
        dry_run=dry_run,
    )

    # -- Manifest report --
    if args.manifest_report:
        manifest_report(manifest)
        return 0

    # -- Run pipelines --
    pipelines_to_run = (
        ["audio", "screen", "transcript"] if args.pipeline == "all" else [args.pipeline]
    )

    all_stats: dict[str, dict] = {}
    exit_code = 0

    for pipeline_name in pipelines_to_run:
        pipeline_cfg = cfg.get(pipeline_name, {})
        if not pipeline_cfg.get("enabled", True):
            logger.info("Pipeline '%s' is disabled in config. Skipping.", pipeline_name)
            continue

        logger.info("=" * 60)
        logger.info("Running pipeline: %s", pipeline_name)
        logger.info("=" * 60)

        try:
            if pipeline_name == "audio":
                from pipelines.audio import run
            elif pipeline_name == "screen":
                from pipelines.screen import run
            elif pipeline_name == "transcript":
                from pipelines.transcript import run
            else:
                logger.error("Unknown pipeline: %s", pipeline_name)
                continue

            stats = run(cfg, dv, blob, manifest)
            all_stats[pipeline_name] = stats
            if stats.get("failed", 0) > 0:
                exit_code = 1  # Partial failure — non-zero exit for alerting

        except Exception as exc:
            logger.exception("Pipeline '%s' crashed: %s", pipeline_name, exc)
            all_stats[pipeline_name] = {"error": str(exc)}
            exit_code = 2

    # -- Final summary --
    print("\n" + "=" * 60)
    print("ARCHIVER RUN COMPLETE")
    print("=" * 60)
    for name, stats in all_stats.items():
        print(f"  {name:12s}: {stats}")
    print()

    if exit_code == 0:
        print("All pipelines completed successfully.")
    elif exit_code == 1:
        print("WARNING: Some records failed — check manifest for details.")
    else:
        print("ERROR: One or more pipelines crashed — check logs.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
