# D365 Contact Center — Media Archiver

Export audio recordings, screen recordings, and transcripts from Dynamics 365 Contact Center to Azure Blob Storage for cost-efficient long-term retention.

## Why this tool?

Dataverse file storage costs ~$10/GB/month. Azure Blob Archive costs ~$0.001/GB/month — a **~10,000x cost difference**. This tool automates the export-archive-cleanup cycle so you pay Dataverse prices only for active data.

| Data type | D365 entity | Retention | Azure tier strategy |
|---|---|---|---|
| Audio recordings | `msdyn_ocrecording` | 3 years | Cool → Archive → Delete at 3yr |
| Screen recordings | `msdyn_screenrecording` | 90 days | Cool → Delete at 90d |
| Transcripts | `annotation` (msdyn_transcript) | 3 years | Cool → Archive → Delete at 3yr |

## Quick Start

### 1. Install dependencies

```bash
cd C:\Code\Tools\d365-media-archiver
pip install -r requirements.txt
```

### 2. Create your config file

```bash
copy config.yaml.example config.yaml
# Edit config.yaml with your D365 org URL, Azure AD app credentials, and storage account
```

### 3. Discover screen recording schema (first time only)

```bash
python archiver.py --config config.yaml --discover-schema
```

Update `config.yaml` → `screen.file_attribute_name` with the discovered value.

### 4. Dry run — preview what would be exported

```bash
python archiver.py --config config.yaml --dry-run
```

Review the log output. No data is downloaded or deleted.

### 5. Apply Azure Blob lifecycle policy

```bash
python archiver.py --config config.yaml --print-lifecycle-policy
# Review the JSON, then apply:
python archiver.py --config config.yaml --apply-lifecycle-policy \
  --subscription <sub-id> \
  --resource-group <rg-name> \
  --storage-account <account-name>
```

Or paste the printed JSON into the Azure portal → Storage Account → Lifecycle Management.

### 6. Run for real

```bash
# All pipelines
python archiver.py --config config.yaml

# Single pipeline
python archiver.py --config config.yaml --pipeline audio
python archiver.py --config config.yaml --pipeline screen
python archiver.py --config config.yaml --pipeline transcript
```

### 7. Check progress

```bash
python archiver.py --config config.yaml --manifest-report
```

---

## Azure Prerequisites

### App Registration

1. Register an app in **Azure Entra ID** (Azure Active Directory)
2. Under **API permissions**, add **Dynamics 365** → `user_impersonation`
3. Create a **client secret** and copy it into `config.yaml`
4. In D365, go to **Settings → Security → Users**, create an **Application User**:
   - Associate it with the App Registration (via Application ID)
   - Assign the **System Administrator** security role (or a custom role with read/delete on recordings/annotations)

### Storage Account

```bash
# Create storage account (Azure CLI)
az storage account create \
  --name <storageaccount> \
  --resource-group <rg> \
  --location eastus \
  --sku Standard_LRS \
  --kind StorageV2

# Create containers
az storage container create --name d365-audio --account-name <storageaccount>
az storage container create --name d365-screen --account-name <storageaccount>
az storage container create --name d365-transcripts --account-name <storageaccount>

# Assign Blob Data Contributor role to your App Registration
az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee <app-registration-client-id> \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<account>
```

---

## Scheduling

### Windows Task Scheduler

```xml
<!-- Run daily at 2:00 AM -->
schtasks /create /tn "D365MediaArchiver" /tr "python C:\Code\Tools\d365-media-archiver\archiver.py --config C:\Code\Tools\d365-media-archiver\config.yaml" /sc daily /st 02:00
```

### Azure Function (Timer trigger)

Deploy `archiver.py` as an Azure Function with a timer trigger:

```python
# function_app.py
import azure.functions as func
import subprocess

app = func.FunctionApp()

@app.function_name("D365MediaArchiver")
@app.timer_trigger(schedule="0 0 2 * * *", arg_name="timer")
def archiver_timer(timer: func.TimerRequest) -> None:
    subprocess.run(["python", "archiver.py", "--config", "config.yaml"], check=True)
```

### cron (Linux/macOS)

```bash
0 2 * * * cd /opt/d365-media-archiver && python archiver.py --config config.yaml >> /var/log/d365-archiver.log 2>&1
```

---

## CLI Reference

```
python archiver.py [options]

Options:
  --config PATH                 Path to config.yaml (required)
  --pipeline {audio,screen,transcript,all}
                                Which pipeline to run (default: all)
  --dry-run                     Query and log, no downloads or deletes
  --discover-schema             Find file attribute names for D365 entities
  --print-lifecycle-policy      Print Azure Blob lifecycle policy JSON
  --apply-lifecycle-policy      Apply lifecycle policy to storage account
    --subscription ID           Azure subscription ID
    --resource-group NAME       Resource group name
    --storage-account NAME      Storage account name
  --manifest-report             Print summary of archived records
```

---

## Architecture

```
D365 Contact Center (Dataverse)
  msdyn_ocrecording      ──> [audio.py]      ──> d365-audio/YYYY/MM/DD/<id>/
  msdyn_screenrecording  ──> [screen.py]     ──> d365-screen/YYYY/MM/DD/<id>/
  annotation             ──> [transcript.py] ──> d365-transcripts/YYYY/MM/DD/<id>/
                                     |
                              [manifest.csv]   (idempotency + audit trail)
                                     |
                          Azure Blob Lifecycle Policy
                            Cool (30d) → Archive (90d) → Delete (3yr)
```

---

## Files

| File | Purpose |
|---|---|
| `archiver.py` | Main entry point, CLI, orchestration |
| `config.py` | Config loader and validator |
| `dataverse_client.py` | MSAL auth, Dataverse Web API, retry/throttle |
| `blob_client.py` | Azure Blob upload, managed identity auth |
| `manifest.py` | CSV manifest, idempotency, progress tracking |
| `lifecycle_policy.py` | Azure Blob lifecycle policy generator/applier |
| `pipelines/audio.py` | Audio recording export pipeline |
| `pipelines/screen.py` | Screen recording export pipeline |
| `pipelines/transcript.py` | Transcript export pipeline |
| `config.yaml.example` | Config template (copy to config.yaml) |
| `D365-Media-Archiver-Design.md` | Full design document with architecture decisions |

---

## Important Notes

> [!WARNING]
> The tool deletes records from Dataverse **only after verifying** the blob upload succeeded and the file size is non-zero. However, always test with `--dry-run` first, and ensure you have a backup strategy before running against production.

> [!NOTE]
> For **audio recordings**, the default `cleanup_action: retain` uses Dataverse LTDR to keep the metadata row (agent name, duration, conversation ID, queue) readable in D365 while removing the file blob. This preserves compliance query capability. Change to `delete` if you don't need the metadata.

> [!NOTE]
> **Screen recording file attribute name** must be discovered before first run using `--discover-schema`. The attribute name is not publicly documented and may vary between org versions.
