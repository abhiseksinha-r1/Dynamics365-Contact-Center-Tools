"""config.py — Load and validate config.yaml for d365-media-archiver."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

REQUIRED_DATAVERSE = ["org_url", "tenant_id", "client_id", "api_version"]
REQUIRED_BLOB = ["account_url", "auth_method"]


class ConfigError(Exception):
    pass


def load(path: str) -> dict:
    """Load and validate config.yaml. Returns the config dict."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ConfigError("Config file is empty or not valid YAML.")

    _validate_dataverse(cfg)
    _validate_blob(cfg)
    _apply_env_overrides(cfg)
    _apply_defaults(cfg)

    return cfg


def _validate_dataverse(cfg: dict) -> None:
    dv = cfg.get("dataverse", {})
    for key in REQUIRED_DATAVERSE:
        if not dv.get(key):
            raise ConfigError(f"dataverse.{key} is required in config.")
    if not dv.get("client_secret") and not dv.get("client_certificate_path"):
        raise ConfigError(
            "dataverse.client_secret or dataverse.client_certificate_path is required."
        )


def _validate_blob(cfg: dict) -> None:
    bs = cfg.get("blob_storage", {})
    for key in REQUIRED_BLOB:
        if not bs.get(key):
            raise ConfigError(f"blob_storage.{key} is required in config.")
    method = bs.get("auth_method", "")
    if method == "connection_string" and not bs.get("connection_string"):
        raise ConfigError("blob_storage.connection_string is required when auth_method=connection_string.")
    if method == "account_key" and not bs.get("account_key"):
        raise ConfigError("blob_storage.account_key is required when auth_method=account_key.")
    containers = bs.get("containers", {})
    for name in ["audio", "screen", "transcripts"]:
        if not containers.get(name):
            raise ConfigError(f"blob_storage.containers.{name} is required.")


def _apply_env_overrides(cfg: dict) -> None:
    """Allow secrets to be injected via environment variables."""
    dv = cfg.setdefault("dataverse", {})
    if os.environ.get("D365_CLIENT_SECRET"):
        dv["client_secret"] = os.environ["D365_CLIENT_SECRET"]
    if os.environ.get("D365_CLIENT_ID"):
        dv["client_id"] = os.environ["D365_CLIENT_ID"]
    if os.environ.get("D365_TENANT_ID"):
        dv["tenant_id"] = os.environ["D365_TENANT_ID"]

    bs = cfg.setdefault("blob_storage", {})
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        bs["connection_string"] = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    if os.environ.get("AZURE_STORAGE_ACCOUNT_KEY"):
        bs["account_key"] = os.environ["AZURE_STORAGE_ACCOUNT_KEY"]


def _apply_defaults(cfg: dict) -> None:
    dv = cfg.setdefault("dataverse", {})
    dv.setdefault("max_retries", 5)
    dv.setdefault("page_size", 100)

    run = cfg.setdefault("run", {})
    run.setdefault("dry_run", False)
    run.setdefault("max_records_per_run", 1000)
    run.setdefault("log_level", "INFO")

    mf = cfg.setdefault("manifest", {})
    mf.setdefault("path", "./manifest/archive_manifest.csv")
    mf.setdefault("append_mode", True)

    for pipeline in ["audio", "screen", "transcript"]:
        p = cfg.setdefault(pipeline, {})
        p.setdefault("enabled", True)

    cfg["audio"].setdefault("export_after_days", 7)
    cfg["audio"].setdefault("cleanup_action", "retain")
    cfg["audio"].setdefault("file_attribute_name", "msdyn_recording")

    cfg["screen"].setdefault("export_after_days", 1)
    cfg["screen"].setdefault("cleanup_action", "delete")
    cfg["screen"].setdefault("file_attribute_name", None)

    cfg["transcript"].setdefault("export_after_days", 7)
    cfg["transcript"].setdefault("cleanup_action", "delete_annotation_keep_metadata")
