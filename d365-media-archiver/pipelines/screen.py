"""pipelines/screen.py — Archive screen recordings from msdyn_screenrecording."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from blob_client import BlobClient
from dataverse_client import DataverseClient, DataverseError
from manifest import Manifest

logger = logging.getLogger(__name__)

ENTITY_SET = "msdyn_screenrecordings"
PIPELINE = "screen"
CONTENT_TYPE = "video/mp4"


def run(cfg: dict, dv: DataverseClient, blob: BlobClient, manifest: Manifest) -> dict:
    """
    Screen recording pipeline: query aged msdyn_screenrecording records,
    download via SAS URL, upload to Azure Blob, delete from Dataverse.

    Returns summary dict: { total, exported, skipped, failed }
    """
    pipeline_cfg = cfg.get("screen", {})
    export_after_days = int(pipeline_cfg.get("export_after_days", 1))
    file_attr = pipeline_cfg.get("file_attribute_name")
    max_records = int(cfg.get("run", {}).get("max_records_per_run", 1000))
    dry_run = cfg.get("run", {}).get("dry_run", False)

    if not file_attr:
        logger.error(
            "[SCREEN] file_attribute_name is not configured. "
            "Run --discover-schema first to find the correct attribute name, "
            "then update config.yaml screen.file_attribute_name."
        )
        return {"total": 0, "exported": 0, "skipped": 0, "failed": 0, "error": "file_attribute_name not set"}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=export_after_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    logger.info(
        "[SCREEN] Starting. export_after_days=%d, cutoff=%s, file_attr=%s",
        export_after_days, cutoff, file_attr,
    )

    stats = {"total": 0, "exported": 0, "skipped": 0, "failed": 0}

    params = {
        "$select": "msdyn_screenrecordingid,msdyn_name,createdon",
        "$filter": f"createdon lt {cutoff}",
        "$top": str(min(dv.page_size, max_records)),
    }

    for record in dv.get_all_pages(ENTITY_SET, params):
        if stats["total"] >= max_records:
            logger.info("[SCREEN] max_records_per_run (%d) reached. Stopping.", max_records)
            break

        stats["total"] += 1
        record_id = record["msdyn_screenrecordingid"]
        name = record.get("msdyn_name", record_id)

        # -- Idempotency check --
        if manifest.already_archived(record_id):
            logger.debug("[SCREEN] Skipping already archived record %s", record_id)
            stats["skipped"] += 1
            continue

        logger.info("[SCREEN] Processing record %s (%s)", record_id, name)

        try:
            # S2 — Get SAS URL
            sas_url, file_name = dv.get_file_sas_url(ENTITY_SET, record_id, file_attr)
            if not sas_url:
                raise DataverseError(f"Empty SAS URL returned for screen recording {record_id}")

            # S3 — Download
            if not dry_run:
                data = dv.download_from_sas(sas_url)
            else:
                data = b""
                logger.info("[DRY-RUN] Would download %s", sas_url)

            # S3b — Build blob path and upload
            created_on = record.get("createdon", "")[:10] or "unknown"
            blob_path = f"screen/{created_on.replace('-', '/')}/{record_id}/{file_name}"

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

            # S4 — Verify
            if not dry_run and len(data) == 0:
                raise DataverseError(f"Downloaded empty screen recording for {record_id}")

            # S4b — Write manifest
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

            # S5 — DELETE from Dataverse (cascades to msdyn_ScreenRecordingLink)
            dv.delete(ENTITY_SET, record_id)
            logger.info("[SCREEN] Deleted record %s from Dataverse", record_id)
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
            logger.error("[SCREEN] Failed record %s: %s", record_id, exc)
            manifest.write(
                pipeline=PIPELINE,
                record_id=record_id,
                status="failed",
                error=str(exc)[:500],
            )
            stats["failed"] += 1

    logger.info("[SCREEN] Done. %s", stats)
    return stats
