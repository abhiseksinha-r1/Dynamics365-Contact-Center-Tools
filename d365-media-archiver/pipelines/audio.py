"""pipelines/audio.py — Archive audio recordings from msdyn_ocrecording."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from blob_client import BlobClient
from dataverse_client import DataverseClient, DataverseError
from manifest import Manifest

logger = logging.getLogger(__name__)

ENTITY_SET = "msdyn_ocrecordings"
PIPELINE = "audio"
CONTENT_TYPE = "audio/mpeg"   # D365 stores recordings as MP4/WAV; use generic if unsure


def run(cfg: dict, dv: DataverseClient, blob: BlobClient, manifest: Manifest) -> dict:
    """
    Audio pipeline: query aged msdyn_ocrecording records, download via SAS URL,
    upload to Azure Blob, then retain or delete the Dataverse record.

    Returns summary dict: { total, exported, skipped, failed }
    """
    pipeline_cfg = cfg.get("audio", {})
    export_after_days = int(pipeline_cfg.get("export_after_days", 7))
    cleanup_action = pipeline_cfg.get("cleanup_action", "retain")
    file_attr = pipeline_cfg.get("file_attribute_name", "msdyn_recording")
    max_records = int(cfg.get("run", {}).get("max_records_per_run", 1000))
    dry_run = cfg.get("run", {}).get("dry_run", False)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=export_after_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    logger.info(
        "[AUDIO] Starting. export_after_days=%d, cutoff=%s, cleanup=%s",
        export_after_days, cutoff, cleanup_action,
    )

    stats = {"total": 0, "exported": 0, "skipped": 0, "failed": 0}

    params = {
        "$select": "msdyn_ocrecordingid,msdyn_name,createdon,msdyn_mediauri",
        "$filter": f"createdon lt {cutoff}",
        "$top": str(min(dv.page_size, max_records)),
    }

    for record in dv.get_all_pages(ENTITY_SET, params):
        if stats["total"] >= max_records:
            logger.info("[AUDIO] max_records_per_run (%d) reached. Stopping.", max_records)
            break

        stats["total"] += 1
        record_id = record["msdyn_ocrecordingid"]
        name = record.get("msdyn_name", record_id)

        # -- Idempotency check --
        if manifest.already_archived(record_id):
            logger.debug("[AUDIO] Skipping already archived record %s", record_id)
            stats["skipped"] += 1
            continue

        logger.info("[AUDIO] Processing record %s (%s)", record_id, name)

        try:
            # A2 — Get SAS URL
            sas_url, file_name = dv.get_file_sas_url(ENTITY_SET, record_id, file_attr)
            if not sas_url:
                raise DataverseError(f"Empty SAS URL returned for {record_id}")

            # A3 — Download
            if not dry_run:
                data = dv.download_from_sas(sas_url)
            else:
                data = b""
                logger.info("[DRY-RUN] Would download %s", sas_url)

            # A4 — Build blob path and upload
            created_on = record.get("createdon", "")[:10] or "unknown"  # YYYY-MM-DD
            blob_path = f"audio/{created_on.replace('-', '/')}/{record_id}/{file_name}"

            blob_url, size, checksum = blob.upload(
                pipeline=PIPELINE,
                blob_path=blob_path,
                data=data,
                content_type=CONTENT_TYPE,
                metadata_tags={
                    "d365_record_id": record_id,
                    "d365_pipeline": PIPELINE,
                    "d365_org_url": dv.org_url,
                    "d365_name": name[:128],
                },
            )

            # A5 — Verify (skip in dry-run)
            if not dry_run and len(data) == 0:
                raise DataverseError(f"Downloaded empty file for {record_id}")

            # A6 — Write manifest
            manifest.write(
                pipeline=PIPELINE,
                record_id=record_id,
                blob_url=blob_url,
                blob_path=blob_path,
                file_size_bytes=size,
                checksum_sha256=checksum,
                d365_deleted=False,
                status="archived",
            )

            # A7 — Cleanup in Dataverse
            if cleanup_action == "retain":
                dv.retain(ENTITY_SET, record_id)
                logger.info("[AUDIO] Retained (LTDR) record %s", record_id)
            elif cleanup_action == "delete":
                dv.delete(ENTITY_SET, record_id)
                logger.info("[AUDIO] Deleted record %s from Dataverse", record_id)
                manifest.write(
                    pipeline=PIPELINE,
                    record_id=record_id,
                    blob_url=blob_url,
                    blob_path=blob_path,
                    file_size_bytes=size,
                    checksum_sha256=checksum,
                    d365_deleted=True,
                    status="archived",
                )

            stats["exported"] += 1

        except Exception as exc:
            logger.error("[AUDIO] Failed record %s: %s", record_id, exc)
            manifest.write(
                pipeline=PIPELINE,
                record_id=record_id,
                status="failed",
                error=str(exc)[:500],
            )
            stats["failed"] += 1

    logger.info("[AUDIO] Done. %s", stats)
    return stats
