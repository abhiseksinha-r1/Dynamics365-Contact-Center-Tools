"""lifecycle_policy.py — Generate and apply Azure Blob Storage lifecycle policy."""

from __future__ import annotations

import json
import logging

from azure.identity import DefaultAzureCredential
from azure.mgmt.storage import StorageManagementClient

logger = logging.getLogger(__name__)


def build_policy(cfg: dict) -> dict:
    """Return the lifecycle policy dict based on retention config."""
    audio_pfx = cfg["blob_storage"]["containers"]["audio"] + "/"
    screen_pfx = cfg["blob_storage"]["containers"]["screen"] + "/"
    transcript_pfx = cfg["blob_storage"]["containers"]["transcripts"] + "/"

    return {
        "rules": [
            {
                "name": "audio-tiering",
                "enabled": True,
                "type": "Lifecycle",
                "definition": {
                    "filters": {
                        "blobTypes": ["blockBlob"],
                        "prefixMatch": [audio_pfx],
                    },
                    "actions": {
                        "baseBlob": {
                            "tierToCool":    {"daysAfterCreationGreaterThan": 30},
                            "tierToArchive": {"daysAfterCreationGreaterThan": 90},
                            "delete":        {"daysAfterCreationGreaterThan": 1095},
                        }
                    },
                },
            },
            {
                "name": "screen-deletion",
                "enabled": True,
                "type": "Lifecycle",
                "definition": {
                    "filters": {
                        "blobTypes": ["blockBlob"],
                        "prefixMatch": [screen_pfx],
                    },
                    "actions": {
                        "baseBlob": {
                            "delete": {"daysAfterCreationGreaterThan": 90},
                        }
                    },
                },
            },
            {
                "name": "transcript-tiering",
                "enabled": True,
                "type": "Lifecycle",
                "definition": {
                    "filters": {
                        "blobTypes": ["blockBlob"],
                        "prefixMatch": [transcript_pfx],
                    },
                    "actions": {
                        "baseBlob": {
                            "tierToCool":    {"daysAfterCreationGreaterThan": 30},
                            "tierToArchive": {"daysAfterCreationGreaterThan": 90},
                            "delete":        {"daysAfterCreationGreaterThan": 1095},
                        }
                    },
                },
            },
        ]
    }


def print_policy(cfg: dict) -> None:
    policy = build_policy(cfg)
    print(json.dumps(policy, indent=2))


def apply_policy(cfg: dict, subscription_id: str, resource_group: str, account_name: str) -> None:
    """
    Apply the lifecycle policy to the storage account via Azure Management API.
    Requires azure-mgmt-storage: pip install azure-mgmt-storage
    """
    try:
        from azure.mgmt.storage.models import ManagementPolicy, ManagementPolicySchema
    except ImportError:
        logger.error(
            "azure-mgmt-storage is required to apply lifecycle policies programmatically. "
            "Run: pip install azure-mgmt-storage\n"
            "Alternatively, paste the JSON output of --print-lifecycle-policy into the Azure portal."
        )
        return

    policy = build_policy(cfg)
    credential = DefaultAzureCredential()
    client = StorageManagementClient(credential, subscription_id)

    client.management_policies.create_or_update(
        resource_group_name=resource_group,
        account_name=account_name,
        management_policy_name="default",
        properties=ManagementPolicySchema(rules=policy["rules"]),
    )
    logger.info(
        "Lifecycle policy applied to storage account '%s' in resource group '%s'.",
        account_name, resource_group,
    )
