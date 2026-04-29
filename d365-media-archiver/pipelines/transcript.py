"""pipelines/transcript.py — Archive transcripts from annotation (msdyn_transcript)."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

from blob_client import BlobClient
from dataverse_client import DataverseClient, DataverseError
from manifest import Manifest

logger = logging.getLogger(__name__)

PIPELINE = "transcript"
CONTENT_TYPE = "application/json"


def run(cfg: dict, dv: DataverseClient, blob: BlobClient, manifest: Manifest) -> dict:
    """
    Transcript pipeline:
      - Query annotation records where objecttypecode = 'msdyn_transcript'
      - Decode base64 documentbody → JSON
      - Upload decoded JSON to Azure Blob
      - Delete the annotation (large blob) — keep parent msdyn_transcript metadata row
      - Optionally RETAIN the msdyn_transcript parent via LTDR

    Returns summary dict: { total, exported, skipped, failed }
    """
    pipeline_cfg = cfg.get("transcript", {})
    export_after_days = int(pipeline_cfg.get("export_after_days", 7))
    cleanup_action = pipeline_cfg.get("cleanup_action", "delete_annotation_keep_metadata")
    max_records = int(cfg.get("run", {}).get("max_records_per_run", 1000))
    dry_run = cfg.get("run", {}).get("dry_run", False)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=export_after_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    logger.info(
        "[TRANSCRIPT] Starting. export_after_days=%d, cutoff=%s, cleanup=%s",
        export_after_days, cutoff, cleanup_action,
    )

    stats = {"total": 0, "exported": 0, "skipped": 0, "failed": 0}

    params = {
        "$select": (
            "annotationid,documentbody,filename,subject,createdon,"
            "_objectid_value,objecttypecode"
        ),
        "$filter": f"objecttypecode eq 'msdyn_transcript' and createdon lt {cutoff}",
        "$top": str(min(dv.page_size, max_records)),
    }

    for record in dv.get_all_pages("annotations", params):
        if stats["total"] >= max_records:
            logger.info("[TRANSCRIPT] max_records_per_run (%d) reached. Stopping.", max_records)
            break

        stats["total"] += 1
        annotation_id = record["annotationid"]
        transcript_id = record.get("_objectid_value", "unknown")
        subject = record.get("subject", annotation_id)

        # -- Idempotency check --
        if manifest.already_archived(annotation_id):
            logger.debug("[TRANSCRIPT] Skipping already archived annotation %s", annotation_id)
            stats["skipped"] += 1
            continue

        logger.info("[TRANSCRIPT] Processing annotation %s (transcript %s)", annotation_id, transcript_id)

        try:
            # T2 — Decode base64 content
            raw_b64 = record.get("documentbody", "")
            if not raw_b64:
                raise DataverseError(f"Empty documentbody for annotation {annotation_id}")

            raw_bytes = base64.b64decode(raw_b64)
            # Validate it's parseable JSON (native D365 transcript format)
            try:
                messages = json.loads(raw_bytes)
            except json.JSONDecodeError:
                # Some transcripts may be plain text or other formats — store as-is
                messages = raw_bytes.decode("utf-8", errors="replace")

            # T3 — Prepare upload content
            if isinstance(messages, list):
                content_bytes = json.dumps(messages, ensure_ascii=False, indent=2).encode("utf-8")
            else:
                content_bytes = str(messages).encode("utf-8")

            # T3b — Build blob path and upload
            created_on = record.get("createdon", "")[:10] or "unknown"
            file_name = record.get("filename", "transcript.json") or "transcript.json"
            blob_path = f"transcripts/{created_on.replace('-', '/')}/{transcript_id}/{annotation_id}/{file_name}"

            blob_url, size, checksum = blob.upload(
                pipeline=PIPELINE,
                blob_path=blob_path,
                data=content_bytes,
                content_type=CONTENT_TYPE,
                metadata_tags={
                    "d365_annotation_id": annotation_id,
                    "d365_transcript_id": transcript_id,
                    "d365_pipeline": PIPELINE,
                    "d365_org_url": dv.org_url,
                    "d365_subject": subject[:128],
                },
            )

            # T4 — Write manifest
            manifest.write(
                pipeline=PIPELINE,
                record_id=annotation_id,
                blob_url=blob_url,
                blob_path=blob_path,
                file_size_bytes=size,
                checksum_sha256=checksum,
                d365_deleted=False,
                status="archived",
            )

            # T5 — Cleanup
            if cleanup_action in ("delete_annotation_keep_metadata", "delete"):
                # Delete the annotation (large base64 blob) — this is the cost-saving step
                dv.delete("annotations", annotation_id)
                logger.info("[TRANSCRIPT] Deleted annotation %s from Dataverse", annotation_id)

            if cleanup_action == "retain_parent":
                # Optionally RETAIN the msdyn_transcript parent row via LTDR
                if transcript_id and transcript_id != "unknown":
                    dv.retain("msdyn_transcripts", transcript_id)
                    logger.info("[TRANSCRIPT] Retained msdyn_transcript %s via LTDR", transcript_id)

            manifest.write(
                pipeline=PIPELINE,
                record_id=annotation_id,
                blob_url=blob_url,
                blob_path=blob_path,
                file_size_bytes=size,
                checksum_sha256=checksum,
                d365_deleted=True,
                status="archived",
            )

            stats["exported"] += 1

        except Exception as exc:
            logger.error("[TRANSCRIPT] Failed annotation %s: %s", annotation_id, exc)
            manifest.write(
                pipeline=PIPELINE,
                record_id=annotation_id,
                status="failed",
                error=str(exc)[:500],
            )
            stats["failed"] += 1

    logger.info("[TRANSCRIPT] Done. %s", stats)
    return stats
