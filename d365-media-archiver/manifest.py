"""manifest.py — CSV manifest for tracking archived records and idempotency."""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FIELDNAMES = [
    "pipeline",
    "record_id",
    "blob_url",
    "blob_path",
    "exported_at",
    "file_size_bytes",
    "checksum_sha256",
    "d365_deleted",
    "status",          # archived | failed | skipped
    "error",
]


class Manifest:
    def __init__(self, path: str, append_mode: bool = True, dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.append_mode = append_mode
        self._archived: set[str] = set()   # record_ids already successfully archived
        self._rows: list[dict] = []

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def already_archived(self, record_id: str) -> bool:
        return record_id in self._archived

    def write(
        self,
        *,
        pipeline: str,
        record_id: str,
        blob_url: str = "",
        blob_path: str = "",
        file_size_bytes: int = 0,
        checksum_sha256: str = "",
        d365_deleted: bool = False,
        status: str = "archived",
        error: str = "",
    ) -> None:
        row = {
            "pipeline": pipeline,
            "record_id": record_id,
            "blob_url": blob_url,
            "blob_path": blob_path,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "file_size_bytes": file_size_bytes,
            "checksum_sha256": checksum_sha256,
            "d365_deleted": str(d365_deleted),
            "status": status,
            "error": error,
        }
        if status == "archived":
            self._archived.add(record_id)

        if self.dry_run:
            logger.debug("[DRY-RUN] manifest row: %s", row)
            return

        file_exists = self.path.exists() and self.path.stat().st_size > 0
        mode = "a" if (self.append_mode and file_exists) else "w"
        with open(self.path, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if mode == "w" or not file_exists:
                writer.writeheader()
            writer.writerow(row)

    def summary(self) -> dict:
        counts: dict[str, dict] = {}
        for row in self._rows:
            p = row["pipeline"]
            if p not in counts:
                counts[p] = {"archived": 0, "failed": 0, "skipped": 0, "total_bytes": 0}
            counts[p][row.get("status", "unknown")] = (
                counts[p].get(row.get("status", "unknown"), 0) + 1
            )
            try:
                counts[p]["total_bytes"] += int(row.get("file_size_bytes") or 0)
            except (ValueError, TypeError):
                pass
        return counts

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self._rows.append(row)
                    if row.get("status") == "archived":
                        self._archived.add(row["record_id"])
            logger.info(
                "Manifest loaded: %d total rows, %d already archived.",
                len(self._rows),
                len(self._archived),
            )
        except Exception as exc:
            logger.warning("Could not load existing manifest: %s", exc)
