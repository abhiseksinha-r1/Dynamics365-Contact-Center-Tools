# D365 Contact Center — Media Archiver Design Document

**Version:** 1.0  
**Status:** Draft  
**Scope:** Audio recordings, screen recordings, and transcripts in Dynamics 365 Contact Center  

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Why OOTB Options Are Insufficient](#2-why-ootb-options-are-insufficient)
3. [Storage Architecture in D365 Contact Center](#3-storage-architecture-in-d365-contact-center)
4. [Retention Requirements](#4-retention-requirements)
5. [Solution Architecture](#5-solution-architecture)
6. [Pipelines](#6-pipelines)
   - [6.1 Audio Pipeline](#61-audio-pipeline)
   - [6.2 Screen Recording Pipeline](#62-screen-recording-pipeline)
   - [6.3 Transcript Pipeline](#63-transcript-pipeline)
7. [Azure Blob Storage Tiering Strategy](#7-azure-blob-storage-tiering-strategy)
8. [Idempotency and State Tracking](#8-idempotency-and-state-tracking)
9. [Cleanup Strategy](#9-cleanup-strategy)
10. [Tool Design — Scripts and Components](#10-tool-design--scripts-and-components)
11. [Configuration Reference](#11-configuration-reference)
12. [Azure Prerequisites](#12-azure-prerequisites)
13. [Sequencing and Deployment](#13-sequencing-and-deployment)
14. [Risks and Mitigations](#14-risks-and-mitigations)
15. [Open Questions](#15-open-questions)

---

## 1. Problem Statement

Dynamics 365 Contact Center accumulates three categories of binary media per conversation:

| Category | Approximate size | Accumulation rate (example: 1,000 calls/day) |
|---|---|---|
| Audio recording | ~10 MB per 20-min call | ~10 GB/day |
| Screen recording | ~100–500 MB per session | ~100–500 GB/day |
| Transcript | ~40 KB per 20-min call | ~40 MB/day |

All of this data lives in **Dataverse file storage**, which is priced significantly higher than Azure Blob Storage. Customers need:

- **Audio**: accessible for 3 years (compliance, legal hold, quality review)
- **Screen recordings**: accessible for 90 days (QA, training, dispute resolution), then deletable
- **Transcripts**: long-term retention for analytics and compliance

There is no OOTB mechanism to **export and tier** this data to lower-cost Azure storage automatically.

---

## 2. Why OOTB Options Are Insufficient

### Power Platform Long Term Data Retention (LTDR)

LTDR retains Dataverse rows in a compressed read-only state within Dataverse. It does **not** move data out of Dataverse and therefore does **not** reduce Azure file storage costs:

> *"For file and image attachments, Dataverse long term retention doesn't reduce capacity consumed."*
> — [Microsoft Docs: Dataverse long term data retention overview](https://learn.microsoft.com/power-apps/maker/data-platform/data-retention-overview)

`msdyn_ocrecording` does support the `Retain` message, meaning LTDR can be applied to the **metadata row**. However the actual audio/video file blob cost does not decrease. LTDR is useful as a **complementary step** (keep the metadata read-only in Dataverse after the file is archived externally), not as a standalone cost solution.

### Bulk Record Deletion Jobs

Bulk deletion is **destructive**. It permanently deletes records. It cannot:
- Export before deleting
- Move to lower-cost storage
- Satisfy a "retain for 3 years" legal requirement

Microsoft explicitly recommends Bulk Delete jobs only for **screen recordings and transcripts**, not audio, and only when the data is no longer needed.

### ACS Bring Your Own Storage (BYOS)

ACS BYOS directs **new** recordings to your own Azure Blob container at record time. It does **not** retroactively migrate existing data already in Dataverse. Useful for greenfield deployments but not for existing accumulated data.

---

## 3. Storage Architecture in D365 Contact Center

Understanding where each data type actually lives is essential for building the correct extraction logic.

### Audio Recordings

```
ACS Recording API
    └──> (BYOS not configured) Dataverse fileattachment
              └── msdyn_ocrecording
                    ├── msdyn_recording     (File attribute → actual MP4/WAV blob)
                    ├── msdyn_recordingmetadata  (File attribute → ACS metadata JSON)
                    ├── msdyn_mediauri      (String → URI, may point to ACS or Dataverse)
                    └── msdyn_recordingtarget → msdyn_ocliveworkitem (parent conversation)
```

Retrieval: `GetFileSasUrl` Dataverse API generates a time-limited SAS URL for each file attribute. SAS URLs expire within minutes — must be used immediately.

### Screen Recordings

```
Desktop Companion App
    └──> Dataverse fileattachment
              └── msdyn_ScreenRecording
                    ├── (file attribute — name TBC, verify per org schema)
                    └── msdyn_ScreenRecordingLink  (child entity — chunk links)
```

> ⚠️ Microsoft documentation for screen recording export API is sparse. The `msdyn_ScreenRecording` entity likely has a file attribute similar to `msdyn_ocrecording`. Schema must be verified against each org using `GET /api/data/v9.2/EntityDefinitions(LogicalName='msdyn_screenrecording')/Attributes` before running.

### Transcripts

```
Omnichannel transcript engine
    └──> Dataverse
              └── msdyn_transcript (metadata)
                    └── annotation (child Note)
                          ├── documentbody  (base64-encoded JSON array of messages)
                          ├── objecttypecode = 'msdyn_transcript'
                          └── filename = 'transcript.json'
```

Retrieval: Standard Dataverse annotation API — no SAS URL needed. The `documentbody` field contains the base64-encoded JSON directly in the API response.

---

## 4. Retention Requirements

| Data type | Retain for | After retention period | Priority |
|---|---|---|---|
| Audio recordings | **3 years** | Delete from Azure Blob (Archive tier) | High — largest cost driver |
| Screen recordings | **90 days** | Delete from Dataverse and Azure Blob | High — largest volume |
| Transcripts | **3 years** (recommended) | Customer-defined | Medium |

### Tiering targets per data type

| Data type | Day 0–30 | Day 30–90 | Day 90–365 | Day 365–3yr | After 3yr |
|---|---|---|---|---|---|
| Audio | Cool | Cool | Archive | Archive | Delete |
| Screen recordings | Cool | Delete after 90d | — | — | — |
| Transcripts | Cool | Cool | Archive | Archive | Delete |

---

## 5. Solution Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  D365 Contact Center (Dataverse)                                            │
│                                                                             │
│   msdyn_ocrecording  ──────────────────────────────────┐                   │
│   msdyn_ScreenRecording  ──────────────────────────────┤                   │
│   msdyn_transcript + annotation  ──────────────────────┤                   │
└────────────────────────────────────────────────────────┼────────────────────┘
                                                         │ Dataverse Web API
                                                         ▼
                                           ┌─────────────────────────┐
                                           │  d365-media-archiver    │
                                           │  (Python script or      │
                                           │   Azure Function)       │
                                           │                         │
                                           │  1. Query aged records  │
                                           │  2. Download via SAS    │
                                           │  3. Upload to blob      │
                                           │  4. Verify checksum     │
                                           │  5. Write manifest      │
                                           │  6. Delete/Retain in DV │
                                           └────────────┬────────────┘
                                                        │
                        ┌───────────────────────────────┼────────────────────┐
                        │                               │                    │
                        ▼                               ▼                    ▼
          ┌─────────────────────┐         ┌─────────────────────┐  ┌─────────────────┐
          │ Azure Blob Storage  │         │ Manifest / Catalog   │  │ Dataverse LTDR  │
          │                     │         │ (local CSV or Azure  │  │ (metadata rows  │
          │ /audio/YYYY/MM/DD/  │         │  Table Storage)      │  │  retained for   │
          │ /screen/YYYY/MM/DD/ │         │                      │  │  compliance     │
          │ /transcripts/...    │         │ record_id, blob_url,  │  │  queries)       │
          │                     │         │ exported_at, size,   │  └─────────────────┘
          │ Lifecycle policy:   │         │ checksum, status     │
          │  Cool → Archive     │         └─────────────────────┘
          │  Archive → Delete   │
          └─────────────────────┘
```

### Deployment modes

| Mode | Use case |
|---|---|
| **CLI / cron** | Customer runs on a VM or on-premises server on a schedule |
| **Azure Function (Timer trigger)** | Fully cloud-native, serverless, scales automatically |
| **Azure Data Factory pipeline** | Enterprise-grade orchestration, built-in monitoring |

The tool will be designed as a standalone Python script that can be wrapped in any of these modes.

---

## 6. Pipelines

### 6.1 Audio Pipeline

**Trigger:** Records in `msdyn_ocrecordings` where `createdon` < (today − `AUDIO_EXPORT_AFTER_DAYS`)

**Steps:**

```
Step A1 — Query
  GET /api/data/v9.2/msdyn_ocrecordings
    ?$select=msdyn_ocrecordingid,msdyn_name,createdon,msdyn_recordingtarget,msdyn_mediauri
    &$filter=createdon lt <cutoff_date>
    &$top=100   (paginate with @odata.nextLink)

Step A2 — Get SAS URL (per record)
  POST /api/data/v9.2/GetFileSasUrl(
    Target=@p1, FileAttributeName='msdyn_recording'
  )?@p1={"@odata.id":"msdyn_ocrecordings(<id>)"}
  Response: { "Result": { "SasUrl": "...", "FileName": "..." } }

Step A3 — Download
  HTTP GET <SasUrl>  →  stream to temp buffer

Step A4 — Upload to Azure Blob
  Container: <AUDIO_BLOB_CONTAINER>
  Path: audio/YYYY/MM/DD/<msdyn_ocrecordingid>/<FileName>
  Tier: Cool
  Metadata tags: { "d365_record_id": "...", "conversation_id": "...", "exported_at": "..." }

Step A5 — Verify
  Compare downloaded file size == uploaded blob size
  (Optional: SHA-256 checksum)

Step A6 — Write manifest row
  { "pipeline": "audio", "record_id": "...", "blob_url": "...",
    "exported_at": "...", "file_size_bytes": ..., "status": "archived" }

Step A7 — Cleanup in Dataverse
  Option A (recommended): POST /Retain on the msdyn_ocrecording record
    → metadata row kept read-only in Dataverse LTDR
    → file blob association removed (PurgeRetainedContent later)
  Option B: DELETE /api/data/v9.2/msdyn_ocrecordings(<id>)
    → full deletion (use only if metadata not needed for compliance)
```

**Error handling:** Failed records are written to the manifest with `status: failed` and skipped. The next run retries them.

---

### 6.2 Screen Recording Pipeline

**Trigger:** Records in `msdyn_screenrecordings` where `createdon` < (today − `SCREEN_EXPORT_AFTER_DAYS`, default: 0 — export immediately for cost savings, delete after 90d)

> ⚠️ **Pre-run requirement:** Verify the file attribute name for `msdyn_ScreenRecording` in your org:
> ```
> GET /api/data/v9.2/EntityDefinitions(LogicalName='msdyn_screenrecording')/Attributes
>   ?$filter=AttributeType eq Microsoft.Dynamics.CRM.AttributeTypeCode'File'
>   &$select=LogicalName,DisplayName
> ```

**Steps:**

```
Step S1 — Query
  GET /api/data/v9.2/msdyn_screenrecordings
    ?$select=msdyn_screenrecordingid,msdyn_name,createdon
    &$filter=createdon lt <cutoff_date>
    &$top=100

Step S2 — Get SAS URL (per record, using discovered file attribute name)
  POST /api/data/v9.2/GetFileSasUrl(
    Target=@p1, FileAttributeName='<file_attr_name>'
  )?@p1={"@odata.id":"msdyn_screenrecordings(<id>)"}

Step S3 — Download + Upload to Azure Blob
  Container: <SCREEN_BLOB_CONTAINER>
  Path: screen/YYYY/MM/DD/<msdyn_screenrecordingid>/<FileName>
  Tier: Cool

Step S4 — Write manifest row

Step S5 — DELETE from Dataverse (also deletes msdyn_ScreenRecordingLink children via cascade)
  DELETE /api/data/v9.2/msdyn_screenrecordings(<id>)
  Note: Unlike audio, screen recordings don't need metadata retention in D365.
  Use Bulk Delete job for the final cleanup after the retention window.
```

**Scheduled deletion job:** After 90 days, a separate Azure Blob lifecycle policy rule deletes the blobs. No second pass needed from the script.

---

### 6.3 Transcript Pipeline

**Trigger:** `annotation` records where `objecttypecode = 'msdyn_transcript'` and `createdon` < (today − `TRANSCRIPT_EXPORT_AFTER_DAYS`)

**Steps:**

```
Step T1 — Query
  GET /api/data/v9.2/annotations
    ?$select=annotationid,documentbody,filename,subject,createdon,
             objectid_msdyn_transcript
    &$filter=objecttypecode eq 'msdyn_transcript'
             and createdon lt <cutoff_date>
    &$top=100

Step T2 — Decode content
  raw_bytes = base64.b64decode(record['documentbody'])
  messages = json.loads(raw_bytes)

Step T3 — Upload to Azure Blob
  Container: <TRANSCRIPT_BLOB_CONTAINER>
  Path: transcripts/YYYY/MM/DD/<msdyn_transcript_id>/transcript.json
  Content: the decoded JSON (re-encoded as UTF-8, NOT re-base64'd)
  Tier: Cool

Step T4 — Write manifest row

Step T5 — DELETE annotation from Dataverse (content gone, saves storage)
  DELETE /api/data/v9.2/annotations(<annotationid>)
  Note: The parent msdyn_transcript metadata row is kept — it's tiny and
        useful for audit (shows "transcript existed for conversation X").
        Optionally RETAIN the msdyn_transcript row via LTDR.
```

---

## 7. Azure Blob Storage Tiering Strategy

### Recommended container layout

```
storage account: <customer>-d365-archive
  containers:
    d365-audio/
      audio/2024/01/15/<recording-id>/call_recording.mp4
    d365-screen/
      screen/2024/01/15/<recording-id>/screen_recording.mp4
    d365-transcripts/
      transcripts/2024/01/15/<transcript-id>/transcript.json
```

### Azure Blob Lifecycle Policy (ARM / JSON)

Apply this policy once to the storage account. It handles all tiering and deletion automatically:

```json
{
  "rules": [
    {
      "name": "audio-tiering",
      "enabled": true,
      "type": "Lifecycle",
      "definition": {
        "filters": { "blobTypes": ["blockBlob"], "prefixMatch": ["d365-audio/"] },
        "actions": {
          "baseBlob": {
            "tierToCool":    { "daysAfterCreationGreaterThan": 30 },
            "tierToArchive": { "daysAfterCreationGreaterThan": 90 },
            "delete":        { "daysAfterCreationGreaterThan": 1095 }
          }
        }
      }
    },
    {
      "name": "screen-deletion",
      "enabled": true,
      "type": "Lifecycle",
      "definition": {
        "filters": { "blobTypes": ["blockBlob"], "prefixMatch": ["d365-screen/"] },
        "actions": {
          "baseBlob": {
            "delete": { "daysAfterCreationGreaterThan": 90 }
          }
        }
      }
    },
    {
      "name": "transcript-tiering",
      "enabled": true,
      "type": "Lifecycle",
      "definition": {
        "filters": { "blobTypes": ["blockBlob"], "prefixMatch": ["d365-transcripts/"] },
        "actions": {
          "baseBlob": {
            "tierToCool":    { "daysAfterCreationGreaterThan": 30 },
            "tierToArchive": { "daysAfterCreationGreaterThan": 90 },
            "delete":        { "daysAfterCreationGreaterThan": 1095 }
          }
        }
      }
    }
  ]
}
```

> [!NOTE]
> Azure Archive tier blobs require **rehydration** (hours) before they can be read. Ensure your compliance/legal review process accounts for this. Set a rehydration priority policy if SLA-bound retrieval is needed.

### Cost comparison (rough estimates, East US, April 2025)

| Storage tier | $/GB/month | 10 TB audio cost/month |
|---|---|---|
| Dataverse file storage | ~$10.00 | ~$100,000 |
| Azure Blob Hot | $0.018 | $180 |
| Azure Blob Cool | $0.01 | $100 |
| Azure Blob Archive | $0.00099 | ~$10 |

Even moving to Hot tier in Azure Blob is a **~550x cost reduction** versus Dataverse file storage.

---

## 8. Idempotency and State Tracking

To make the tool safe to re-run (network failures, rate limits, partial runs):

### Manifest file

Each run appends to a **manifest CSV/JSON**:

```csv
pipeline,record_id,blob_url,exported_at,file_size_bytes,checksum_sha256,d365_deleted,status
audio,<guid>,https://storage.../audio/...,2025-01-15T10:00:00Z,10485760,abc123...,true,archived
transcript,<guid>,https://storage.../transcripts/...,2025-01-15T10:01:00Z,40960,def456...,false,failed
```

### Skip logic

Before downloading, check if record_id already appears in the manifest with `status=archived`. Skip if found.

### Blob metadata tags

Each uploaded blob carries Dataverse metadata as Azure blob tags:
```
d365_record_id: <guid>
d365_pipeline: audio|screen|transcript
d365_org_url: https://<org>.crm.dynamics.com
d365_conversation_id: <msdyn_ocliveworkitemid>
exported_at: ISO-8601
```

This enables **reverse lookup**: given a blob, find the original D365 conversation.

---

## 9. Cleanup Strategy

### Recommended approach per data type

| Data type | After successful export | Rationale |
|---|---|---|
| **Audio** | `POST /Retain` on `msdyn_ocrecording` | Preserves metadata row for compliance queries ("was this call recorded?"). Use `PurgeRetainedContent` if storage savings needed immediately. |
| **Screen recordings** | `DELETE /msdyn_screenrecordings(<id>)` | No long-term compliance value in metadata. Full delete is appropriate. |
| **Transcripts** | `DELETE /annotations(<id>)`, keep `msdyn_transcript` parent | Removes the large base64 blob. The tiny metadata row confirms the conversation had a transcript. |

> [!WARNING]
> Always verify the blob exists and the file size matches before deleting from Dataverse. The tool enforces this via Step A5/S4/T4 verification before any delete.

---

## 10. Tool Design — Scripts and Components

```
d365-media-archiver/
│
├── archiver.py                  # Main entry point — CLI flags, orchestration
├── config.py                    # Loads config.yaml, validates required fields
├── dataverse_client.py          # Auth (MSAL), Dataverse API wrapper, retry/throttle
├── blob_client.py               # Azure Blob Storage upload, SAS URL download
├── pipelines/
│   ├── audio.py                 # Audio pipeline (Steps A1–A7)
│   ├── screen.py                # Screen recording pipeline (Steps S1–S5)
│   └── transcript.py            # Transcript pipeline (Steps T1–T5)
├── manifest.py                  # Manifest read/write, idempotency checks
├── lifecycle_policy.py          # Generates/applies Azure Blob lifecycle policy JSON
│
├── config.yaml                  # Customer configuration file (see Section 11)
├── config.yaml.example          # Template with all fields documented
├── requirements.txt             # Python dependencies
│
├── D365-Media-Archiver-Design.md     # This document
└── README.md                         # Quick-start guide
```

### CLI flags

```bash
# Run all three pipelines
python archiver.py --config config.yaml

# Run specific pipeline only
python archiver.py --config config.yaml --pipeline audio
python archiver.py --config config.yaml --pipeline screen
python archiver.py --config config.yaml --pipeline transcript

# Dry run — query and log, no downloads or deletes
python archiver.py --config config.yaml --dry-run

# Generate and apply Azure Blob lifecycle policy
python archiver.py --config config.yaml --apply-lifecycle-policy

# Discover screen recording file attribute name (run once before first use)
python archiver.py --config config.yaml --discover-schema

# Show manifest summary
python archiver.py --config config.yaml --manifest-report
```

---

## 11. Configuration Reference

```yaml
# config.yaml

# --- Dynamics 365 / Dataverse ---
dataverse:
  org_url: "https://<your-org>.crm.dynamics.com"
  tenant_id: "<AAD tenant GUID>"
  client_id: "<App Registration client ID>"
  client_secret: "<secret>"          # Or use client_certificate_path below
  # client_certificate_path: "path/to/cert.pem"
  api_version: "9.2"
  max_retries: 5
  page_size: 100                     # Records per API page

# --- Azure Blob Storage ---
blob_storage:
  account_url: "https://<storageaccount>.blob.core.windows.net"
  # Authentication: one of managed_identity, connection_string, or account_key
  auth_method: "managed_identity"    # Recommended for Azure-hosted deployments
  # connection_string: "DefaultEndpointsProtocol=https;..."
  # account_key: "<key>"
  containers:
    audio: "d365-audio"
    screen: "d365-screen"
    transcripts: "d365-transcripts"

# --- Pipeline: Audio ---
audio:
  enabled: true
  export_after_days: 7              # Export recordings older than this many days
  cleanup_action: "retain"          # "retain" (LTDR) or "delete"
  file_attribute_name: "msdyn_recording"   # Verified from schema

# --- Pipeline: Screen Recordings ---
screen:
  enabled: true
  export_after_days: 1              # Export as soon as possible to save cost
  cleanup_action: "delete"
  file_attribute_name: null         # null = auto-discover via --discover-schema

# --- Pipeline: Transcripts ---
transcript:
  enabled: true
  export_after_days: 7
  cleanup_action: "delete_annotation_keep_metadata"

# --- Manifest ---
manifest:
  path: "./manifest/archive_manifest.csv"
  append_mode: true                 # false = overwrite each run (not recommended)

# --- Run behavior ---
run:
  dry_run: false                    # true = no writes/deletes, log only
  max_records_per_run: 1000         # Safety cap per pipeline per run
  log_level: "INFO"                 # DEBUG, INFO, WARNING, ERROR
```

---

## 12. Azure Prerequisites

### App Registration (for Dataverse access)

1. Register an app in Azure Entra ID
2. Grant **Dataverse API** permissions: `user_impersonation` (or application-level with D365 admin consent)
3. Create an application user in D365 with the **System Administrator** or custom role with:
   - Read/Delete on `msdyn_ocrecording`
   - Read/Delete on `msdyn_ScreenRecording`
   - Read/Delete on `annotation`
   - Read/Retain on `msdyn_transcript`

### Storage Account

1. Create an Azure Storage Account (Standard, LRS minimum, ZRS recommended)
2. Create three containers: `d365-audio`, `d365-screen`, `d365-transcripts`
3. Assign **Storage Blob Data Contributor** role to:
   - The App Registration above (if using service principal auth), OR
   - The Managed Identity of the Azure Function/VM running the archiver
4. Apply the lifecycle policy (use `--apply-lifecycle-policy` flag)

### Network

- The machine/function running the archiver must reach:
  - `https://<org>.crm.dynamics.com` (Dataverse)
  - `https://login.microsoftonline.com` (MSAL token)
  - `https://<storageaccount>.blob.core.windows.net` (Azure Blob)
  - ACS SAS URLs (dynamic hostnames — `*.communication.azure.com`)

---

## 13. Sequencing and Deployment

### First-time setup

```
1. Create Azure App Registration + D365 application user
2. Create storage account + containers
3. Run: python archiver.py --config config.yaml --discover-schema
   └── Validates connectivity, discovers screen recording file attribute name
4. Update config.yaml with discovered screen file_attribute_name
5. Run: python archiver.py --config config.yaml --dry-run
   └── Review what would be exported (no actual downloads or deletes)
6. Run: python archiver.py --config config.yaml --apply-lifecycle-policy
   └── Applies blob tiering rules to storage account
7. Run: python archiver.py --config config.yaml --pipeline transcript
   └── Start with transcripts (smallest, lowest risk, no SAS URL expiry pressure)
8. Run: python archiver.py --config config.yaml --pipeline audio
9. Run: python archiver.py --config config.yaml --pipeline screen
```

### Ongoing schedule (recommended)

| Pipeline | Frequency | Suggested time |
|---|---|---|
| Audio | Daily | 2:00 AM |
| Screen recordings | Daily | 2:30 AM |
| Transcripts | Daily | 3:00 AM |

Run as:
- **Azure Function** with Timer trigger (`0 0 2 * * *`)
- **cron job** on a VM: `0 2 * * * python /opt/d365-archiver/archiver.py --config config.yaml`
- **Azure Data Factory** pipeline with scheduled trigger

---

## 14. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| SAS URL expires before download completes | Medium | Failed export, record not deleted | Retry with fresh SAS URL on each attempt. Never cache SAS URLs across retries. |
| Dataverse API throttling (429) | High | Slow runs | Exponential backoff with jitter. Run during off-peak hours. Respect `Retry-After` header. |
| Network interruption mid-download | Medium | Partial blob + manifest inconsistency | Verify blob size == expected size before writing manifest as `archived`. Use Azure Blob `PutBlockList` for large files. |
| Screen recording file attribute name unknown | High (first run) | Schema discovery fails | `--discover-schema` flag queries EntityDefinitions API. Fallback: check manually in D365 Solution Explorer. |
| Delete before upload verified | Critical | Permanent data loss | Tool enforces verify-before-delete. `--dry-run` always available. |
| Archive tier retrieval latency | Low | Compliance team can't access audio quickly | Document rehydration SLA (hours). Consider keeping last 90 days in Cool tier. |
| LTDR `Retain` call fails | Low | Metadata not retained | Log and continue — the blob is already archived. Retry LTDR separately. |

---

## 15. Open Questions

> These must be answered before the first production run.

| # | Question | Owner | Notes |
|---|---|---|---|
| 1 | What is the exact file attribute logical name on `msdyn_ScreenRecording`? | Customer / Developer | Run `--discover-schema` to find automatically |
| 2 | Does the customer org use BYOS (ACS Bring Your Own Storage) for audio? If yes, recordings may already be in Azure — need different extraction path | Customer admin | Check ACS resource settings in Azure portal |
| 3 | Are there legal hold requirements that prevent deletion from Dataverse even after archiving? | Customer compliance team | May require keeping `msdyn_ocrecording` in LTDR rather than deleting |
| 4 | Is the customer on a Managed Environment (required for LTDR)? | Customer Power Platform admin | Check PPAC → Environment → Settings → Features |
| 5 | What is the expected daily volume? (calls/day, average call duration, screen recording adoption rate) | Customer ops | Needed for storage sizing and run-time estimates |
| 6 | Are screen recordings stored as a single file or chunked (via `msdyn_ScreenRecordingLink`)? | Developer (test org) | Determines whether pipeline loops over link records or single file |
| 7 | Does the customer need the archived media to be searchable / indexed (e.g., AI-powered search over transcripts)? | Customer | If yes, consider Azure AI Search + Cognitive Services indexer on the blob container instead of plain archival |

---

*Next step: Implement `archiver.py` and all pipeline modules based on this design.*
