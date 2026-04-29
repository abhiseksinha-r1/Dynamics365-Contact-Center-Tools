"""blob_client.py — Azure Blob Storage upload and SAS URL download."""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

logger = logging.getLogger(__name__)


class BlobClient:
    def __init__(self, cfg: dict):
        bs = cfg["blob_storage"]
        self.containers = bs["containers"]
        self.dry_run = cfg.get("run", {}).get("dry_run", False)
        self._service_client = self._build_service_client(bs)

    # ------------------------------------------------------------------ #
    # Auth                                                                 #
    # ------------------------------------------------------------------ #

    def _build_service_client(self, bs: dict) -> BlobServiceClient:
        method = bs.get("auth_method", "managed_identity")
        account_url = bs["account_url"]

        if method == "connection_string":
            return BlobServiceClient.from_connection_string(bs["connection_string"])
        if method == "account_key":
            return BlobServiceClient(
                account_url=account_url,
                credential=bs["account_key"],
            )
        # Default: managed identity / DefaultAzureCredential
        return BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())

    # ------------------------------------------------------------------ #
    # Upload                                                               #
    # ------------------------------------------------------------------ #

    def upload(
        self,
        *,
        pipeline: str,
        blob_path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata_tags: Optional[dict] = None,
    ) -> tuple[str, int, str]:
        """
        Upload bytes to the appropriate container.
        Returns (blob_url, file_size_bytes, sha256_checksum).
        """
        container_name = self.containers[pipeline]
        checksum = hashlib.sha256(data).hexdigest()
        size = len(data)

        if self.dry_run:
            fake_url = f"https://dry-run/{container_name}/{blob_path}"
            logger.info("[DRY-RUN] upload %s → %s (%d bytes)", pipeline, blob_path, size)
            return fake_url, size, checksum

        container_client = self._service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_path)

        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
            tags=metadata_tags or {},
        )

        blob_url = blob_client.url
        logger.info("Uploaded %s bytes → %s", size, blob_url)
        return blob_url, size, checksum

    # ------------------------------------------------------------------ #
    # Existence check                                                      #
    # ------------------------------------------------------------------ #

    def exists(self, pipeline: str, blob_path: str) -> bool:
        """Return True if the blob already exists (idempotency guard)."""
        container_name = self.containers[pipeline]
        try:
            blob_client = self._service_client.get_container_client(container_name).get_blob_client(blob_path)
            blob_client.get_blob_properties()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Container bootstrap                                                  #
    # ------------------------------------------------------------------ #

    def ensure_containers_exist(self) -> None:
        """Create containers if they don't already exist."""
        for name, container_name in self.containers.items():
            try:
                self._service_client.create_container(container_name)
                logger.info("Created container: %s", container_name)
            except Exception:
                pass  # Already exists
