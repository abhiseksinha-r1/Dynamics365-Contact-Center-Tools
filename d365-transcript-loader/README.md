# Load Transcript Data from a Third-Party System into Dynamics 365 Contact Center

This guide explains how to use the `scripts/load_transcripts.py` template to import historical conversation transcripts from an external system (e.g., Zendesk, Genesys, Salesforce Service Cloud, legacy chat platforms) into **Dynamics 365 Contact Center** via the Dataverse Web API.

> [!NOTE]
> This script creates **closed** conversation records. It is intended for historical data migration, not live conversation synchronisation.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Overview](#2-architecture-overview)
3. [Azure AD App Registration](#3-azure-ad-app-registration)
4. [D365 Contact Center Configuration](#4-d365-contact-center-configuration)
5. [Environment Setup](#5-environment-setup)
6. [Source Data Format](#6-source-data-format)
7. [Running the Script](#7-running-the-script)
8. [Field Mapping Reference](#8-field-mapping-reference)
9. [Transcript JSON Schema](#9-transcript-json-schema)
10. [Error Handling and Idempotency](#10-error-handling-and-idempotency)
11. [Customisation Guide](#11-customisation-guide)
12. [Troubleshooting](#12-troubleshooting)
13. [Security Considerations](#13-security-considerations)

---

## 1. Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11 or later |
| Libraries | `msal`, `requests`, `python-dotenv` (see step 5) |
| Azure AD | App registration with a client secret |
| D365 licence | Dynamics 365 Contact Center or Customer Service Enterprise |
| Dataverse permission | The app registration must have `Dynamics CRM` → `user_impersonation` (or a dedicated application user) |

---

## 2. Architecture Overview

```
Third-Party System               Dynamics 365 Contact Center (Dataverse)
(export to .json/.csv)
                                  contact                  (Step A - customer identity)
 conversations ─────────────────> msdyn_ocliveworkitem     (Step D - shell conversation, closed)
  |- messages                           |
  |- customer info                      +──> msdyn_ocsession          (Step E - agent session)
  |- agent info                                   |
  |- channel / timestamps                         +──> msdyn_sessionparticipant  (Step F)
  `- external_id
                                  msdyn_transcript         (Step G - transcript metadata)
                                        |
                                        `──> annotation    (Step H - base64 JSON content)
```

load_transcripts.py creates ALL records (Steps A through H) for every conversation.
OAuth 2.0 client-credentials flow — Dataverse Web API v9.2

### Entities created per conversation

> [!IMPORTANT]
> Because these are **third-party** transcripts, none of these records exist in D365 yet.
> The script creates all of them from scratch as shell records.

| Step | Entity | Purpose | Required for |
|---|---|---|---|
| A | `contact` | Customer identity record | Linking conversation to a known customer |
| D | `msdyn_ocliveworkitem` | Shell conversation (`statecode=1`, `statuscode=4` Closed) | Transcript viewer, case timeline, all analytics |
| E | `msdyn_ocsession` | Agent session within conversation | Session-level metrics, historical analytics |
| F | `msdyn_sessionparticipant` | Agent participation in session | Per-agent handle time, talk time metrics |
| G | `msdyn_transcript` | Transcript metadata record | Anchors the annotation to the conversation |
| H | `annotation` | Base64-encoded JSON transcript content | Actual transcript text visible in the UI |

> [!NOTE]
> Per [Microsoft documentation](https://learn.microsoft.com/dynamics365/customer-service/develop/download-transcripts-bulk),
> transcript content is stored as a **base64-encoded JSON array** in the `annotations` table
> (`documentbody` field) with `objecttypecode = 'msdyn_transcript'`.

---

## 3. Azure AD App Registration

The script authenticates using the **OAuth 2.0 client-credentials flow** (service-to-service, no user sign-in required).

### 3.1 — Register an application

1. Open [Azure Portal → App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps) → **New registration**.
2. Name: e.g. `D365-TranscriptLoader`
3. Supported account type: **Single tenant**
4. Redirect URI: leave blank
5. Click **Register**

### 3.2 — Add Dataverse permission

1. Go to **API permissions → Add a permission → APIs my organization uses**.
2. Search for `Dataverse` and select it.
3. Choose **Delegated permissions** → `user_impersonation`.
4. Click **Grant admin consent**.

### 3.3 — Create a client secret

1. Go to **Certificates & secrets → New client secret**.
2. Set an expiry (12 or 24 months recommended).
3. Copy the **Value** immediately — it is only shown once.

### 3.4 — Note the IDs

| Value | Where to find it |
|---|---|
| `TENANT_ID` | Azure AD → Overview → Tenant ID |
| `CLIENT_ID` | App registration → Overview → Application (client) ID |
| `CLIENT_SECRET` | The secret value you copied above |

---

## 4. D365 Contact Center Configuration

### 4.1 — Create an Application User

Application users let the app registration write to Dataverse without a named user licence.

1. In D365: **Settings → Advanced Settings → Security → Users**.
2. Switch view to **Application Users → New Application User**.
3. Enter the **App ID** (= `CLIENT_ID`).
4. Assign the security role: **Omnichannel Agent** + **Omnichannel Supervisor** (minimum required).

> [!WARNING]
> Do NOT assign the System Administrator role to the application user in production environments.

### 4.2 — Verify required queues exist

The script can optionally link conversations to queues. Verify that the queue names in your source data match exactly the queue names in D365 (**Customer Service Hub → Queues**).

### 4.3 — Note your environment URL

The environment URL has the form `https://<org-name>.crm.dynamics.com`. Find it in:

- Power Platform Admin Center → Environments → select your environment → **Details**.

---

## 5. Environment Setup

### 5.1 — Install Python dependencies

```bash
pip install msal requests python-dotenv
```

### 5.2 — Create .env file

Copy `.env.example` to `.env` and fill in the values:

```ini
# .env
D365_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
D365_CLIENT_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
D365_CLIENT_SECRET=your-client-secret-here
D365_ENVIRONMENT_URL=https://your-org.crm.dynamics.com
```

> [!WARNING]
> Never commit `.env` to source control. The file is excluded in `.gitignore`.

---

## 6. Source Data Format

The script accepts **JSON** or **CSV** files exported from your third-party system.

### 6.1 — JSON format (preferred)

Provide a JSON array where each element is a conversation object:

```json
[
  {
    "external_id":      "CHAT-20240115-001",
    "channel":          "chat",
    "customer_email":   "jane.doe@example.com",
    "customer_name":    "Jane Doe",
    "subject":          "Billing inquiry – invoice #4521",
    "queue_name":       "Billing Support",
    "agent_email":      "agent.smith@contoso.com",
    "start_time":       "2024-01-15T10:00:00Z",
    "end_time":         "2024-01-15T10:22:00Z",
    "language":         "en",
    "messages": [
      {
        "id":           "msg-001",
        "timestamp":    "2024-01-15T10:00:12Z",
        "sender_id":    "jane.doe@example.com",
        "sender_name":  "Jane Doe",
        "role":         "customer",
        "text":         "Hi, I have a question about my invoice."
      },
      {
        "id":           "msg-002",
        "timestamp":    "2024-01-15T10:01:05Z",
        "sender_id":    "agent.smith@contoso.com",
        "sender_name":  "Agent Smith",
        "role":         "agent",
        "text":         "Hello Jane! Could you share the invoice number?"
      }
    ]
  }
]
```

A full sample is provided in `scripts/sample_transcripts.json`.

### 6.2 — CSV format

For CSV, each row is one conversation. The `messages` column must contain a **JSON-encoded string**:

| Column | Type | Required | Notes |
|---|---|---|---|
| `external_id` | string | ✅ | Unique ID in the source system |
| `channel` | string | ✅ | See channel names below |
| `customer_email` | string | ✅ | Used to find/create the Contact |
| `customer_name` | string | ✅ | |
| `start_time` | ISO-8601 | ✅ | UTC recommended |
| `end_time` | ISO-8601 | ✅ | |
| `subject` | string | ✅ | Conversation title |
| `messages` | JSON string | ✅ | Serialised array of message objects |
| `queue_name` | string | ⬜ | Must match D365 queue name exactly |
| `agent_email` | string | ⬜ | Agent's UPN in Azure AD |
| `language` | string | ⬜ | ISO 639-1 code, e.g. `en` |

### 6.3 — Supported channel names

| Source system value | D365 channel code |
|---|---|
| `chat` | 192350000 |
| `sms` | 192350001 |
| `facebook` | 192350002 |
| `whatsapp` | 192350003 |
| `custom` | 192350005 |
| `voice` | 192350008 |
| `email` | 192350012 |
| `teams` | 192350013 |

Update `CHANNEL_MAP` in the script to add or rename values for your source system.

---

## 7. Running the Script

### 7.1 — Dry run (validate without writing)

Always run with `--dry-run` first to validate your source data and configuration without creating any records:

```bash
python scripts/load_transcripts.py --source scripts/sample_transcripts.json --dry-run
```

Expected output:

```
2024-01-20 09:00:01 [INFO] *** DRY-RUN mode — no records will be written to D365 ***
2024-01-20 09:00:01 [INFO] Authenticating with Dataverse (https://your-org.crm.dynamics.com)…
2024-01-20 09:00:02 [INFO] Loaded 2 records from scripts/sample_transcripts.json.
2024-01-20 09:00:02 [INFO] --- Processing record 1/2  (external_id=CHAT-20240115-001) ---
2024-01-20 09:00:02 [INFO] [DRY-RUN] Would create Contact: Jane Doe <jane.doe@example.com>
2024-01-20 09:00:02 [INFO] [DRY-RUN] Would create msdyn_ocliveworkitem: { … }
2024-01-20 09:00:02 [INFO] [DRY-RUN] Would create msdyn_conversationtranscript for work_item <new>.
…
2024-01-20 09:00:03 [INFO] Import complete. Total: 2 | Success: 2 | Skipped: 0 | Errors: 0
```

### 7.2 — Live import

```bash
python scripts/load_transcripts.py --source scripts/sample_transcripts.json
```

### 7.3 — Verbose debug output

```bash
python scripts/load_transcripts.py --source your_export.json --log-level DEBUG
```

### 7.4 — Output files

| File | Contents |
|---|---|
| `load_transcripts.log` | Full run log (appended each run) |
| `load_transcripts_errors.json` | Records that failed to import (only created when errors occur) |

---

## 8. Field Mapping Reference

### msdyn_ocliveworkitem (Conversation — Step D)

| D365 field | Source field | Notes |
|---|---|---|
| `subject` | `subject` | **ApplicationRequired** on the Activity base entity |
| `msdyn_title` | `subject` | OC-specific display title |
| `msdyn_channel` | `channel` | Mapped via `CHANNEL_MAP` |
| `actualstart` | `start_time` | Actual conversation start time |
| `actualend` | `end_time` | Actual conversation end time |
| `msdyn_createdon` | `start_time` | OC-specific created-on timestamp |
| `msdyn_closedon` | `end_time` | When the conversation was closed |
| `msdyn_activeduration` | Calculated | Conversation duration in seconds |
| `msdyn_isoutbound` | (hardcoded) | `false` — inbound conversations |
| `statecode` | (hardcoded) | `1` = Inactive (completed activity) |
| `statuscode` | (hardcoded) | `4` = Closed (NOT 5 which is Wrap-up) |
| `overriddencreatedon` | `start_time` | Historical creation date for the record |
| `msdyn_thirdpartyconversationid` | `external_id` | Idempotency key — prevents duplicate imports |
| `customerid_contact@odata.bind` | Resolved from `customer_email` | Link to Contact |
| `msdyn_queueid@odata.bind` | Resolved from `queue_name` | Link to Queue (optional) |
| `ownerid@odata.bind` | Resolved from `agent_email` | Link to SystemUser (optional) |

### msdyn_ocsession (Agent Session — Step E)

| D365 field | Source field | Notes |
|---|---|---|
| `msdyn_name` | `subject` | Session display name |
| `msdyn_sessioncreatedon` | `start_time` | When the session started |
| `msdyn_sessionclosedtime` | `end_time` | When the session ended |
| `msdyn_channel` | `channel` | Mapped via `CHANNEL_MAP` |
| `statecode` | (hardcoded) | `1` = Inactive (closed session) |
| `statuscode` | (hardcoded) | `2` = Closed |
| `msdyn_liveworkitemid@odata.bind` | Created work item GUID | Parent conversation link |
| `msdyn_agentid@odata.bind` | Resolved from `agent_email` | Assigned agent (optional) |

### msdyn_sessionparticipant (Agent Participation — Step F)

| D365 field | Source field | Notes |
|---|---|---|
| `msdyn_joinedon` | `start_time` | When the agent joined |
| `msdyn_activetime` | Calculated | Total active seconds (end − start) |
| `msdyn_activewrapuptime` | (hardcoded) | `0` — no wrap-up time for imports |
| `msdyn_ocsessionid@odata.bind` | Created session GUID | Parent session link |
| `msdyn_agentid@odata.bind` | Resolved from `agent_email` | Agent link (required — skipped if no agent) |

### msdyn_transcript (Transcript Metadata — Step G)

| D365 field | Source field | Notes |
|---|---|---|
| `msdyn_name` | `subject` | Display name for the transcript record |
| `overriddencreatedon` | `start_time` | Historical creation date |
| `msdyn_ocliveworkitemid@odata.bind` | Created work item GUID | Links transcript to conversation |

### annotation (Transcript Content — Step H)

| D365 field | Value | Notes |
|---|---|---|
| `documentbody` | base64(`messages` JSON array) | The actual transcript content |
| `objecttypecode` | `"msdyn_transcript"` | Tells D365 this note belongs to a transcript |
| `objectid_msdyn_transcript@odata.bind` | Created transcript GUID | Links annotation to `msdyn_transcript` |
| `filename` | `"transcript.json"` | |
| `mimetype` | `"application/json"` | |
| `isdocument` | `true` | Required — marks as a file attachment |
| `subject` | `subject` | Note subject |

### contact (Customer — Step A)

| D365 field | Source field | Notes |
|---|---|---|
| `emailaddress1` | `customer_email` | Used as lookup key |
| `firstname` | First word of `customer_name` | |
| `lastname` | Remainder of `customer_name` | |

---

## 9. Transcript JSON Schema

The `annotation.documentbody` field stores a **base64-encoded JSON array** of message objects. This is the native format that the D365 Contact Center Transcript viewer decodes and renders.

> [!NOTE]
> Per [Microsoft documentation](https://learn.microsoft.com/dynamics365/customer-service/develop/download-transcripts-bulk), you can retrieve all transcripts for an org with:
> ```http
> GET /api/data/v9.1/annotations?$filter=objecttypecode eq 'msdyn_transcript'
> ```
> The `documentBody` field in each result is the base64-encoded JSON array below.

**Decoded JSON array (what gets base64-encoded into `documentbody`):**

```json
[
  {
    "id": "1589863384036",
    "content": "Hi, I have a question about my invoice.",
    "contentType": "text",
    "createdDateTime": "2024-01-15T10:00:12+00:00",
    "created": "2024-01-15T10:00:12Z",
    "from": {
      "user": {
        "displayName": "Jane Doe",
        "id": "jane.doe@example.com"
      }
    },
    "likes": [],
    "attachments": [],
    "tags": "",
    "isControlMessage": false
  },
  {
    "id": "1589863392976",
    "content": "Hello Jane! Could you share the invoice number?",
    "contentType": "text",
    "createdDateTime": "2024-01-15T10:01:05+00:00",
    "created": "2024-01-15T10:01:05Z",
    "from": {
      "user": {
        "displayName": "Agent Smith",
        "id": "agent.smith@contoso.com"
      }
    },
    "likes": [],
    "attachments": [],
    "tags": "public",
    "isControlMessage": false
  }
]
```

**`tags` field values:**

| Value | Meaning |
|---|---|
| `"public"` | Agent → Customer message (visible to customer) |
| `"private"` | Agent → Agent message (not visible to customer) |
| `""` | Customer → Agent message |

**`isControlMessage: true`** is used for system events (agent joined, agent left) — set `content` to an XML or text description of the event.

---

## 10. Error Handling and Idempotency

### Idempotency (safe to re-run)

The script checks `msdyn_thirdpartyconversationid` before creating a new `msdyn_ocliveworkitem`. If a record with the same `external_id` already exists, it is **skipped** — the import is safe to run multiple times.

### Retry logic

HTTP failures (5xx, 429 rate-limit) are retried up to **3 times** with exponential back-off (2 s, 4 s, 8 s). Permanent client errors (400, 401, 403) are not retried.

### Failed records

Records that fail after all retries are written to `load_transcripts_errors.json`. Fix the data or configuration issue and re-run — the script will skip already-imported records and only attempt the failed ones.

### Batch throttling

A 2-second pause is inserted every 20 records to stay within Dataverse API rate limits. Adjust `BATCH_SIZE` in the script for faster or slower throughput.

---

## 11. Customisation Guide

### 11.1 — Adapting `load_source_data()` for your export format

If your third-party system uses a different field naming convention, update the `load_source_data()` function to remap column names. For example, for a Zendesk export:

```python
# Inside load_source_data(), after loading raw records:
for rec in records:
    rec["external_id"] = rec.pop("id", "")
    rec["customer_email"] = rec.pop("requester_email", "")
    rec["customer_name"] = rec.pop("requester_name", "")
    rec["start_time"] = rec.pop("created_at", "")
    rec["end_time"] = rec.pop("updated_at", "")
    rec["subject"] = rec.pop("description", "")
    rec["channel"] = "chat"   # Zendesk uses a single channel
```

### 11.2 — Adapting `_normalize_messages()` for your message format

Different platforms store messages in different shapes. Update `_normalize_messages()` to map your schema:

```python
# Example: Genesys Cloud message format
def _normalize_messages(raw_messages):
    return [
        {
            "id": msg["messageId"],
            "content": msg["body"],
            "contentType": "text",
            "createdDateTime": msg["timestamp"],
            "created": msg["timestamp"],
            "from": {
                "user": {
                    "displayName": msg["sender"]["name"],
                    "id": msg["sender"]["id"],
                }
            },
            "likes": [],
            "attachments": [],
            "tags": "public" if msg["sender"]["type"] == "AGENT" else "",
            "isControlMessage": False,
        }
        for msg in raw_messages
    ]
```

### 11.3 — Adding custom D365 fields

To populate additional fields on `msdyn_ocliveworkitem`, extend the `payload` dict in `create_conversation()`:

```python
# Example: set a custom attribute
payload["new_importedfromthirdparty"] = True
payload["msdyn_customer_sentiment_label"] = record.get("sentiment", "Neutral")
```

### 11.4 — Large-scale imports (100k+ conversations)

For very large datasets:

1. Split the source file into chunks of 5,000 records.
2. Run the script in parallel across chunks (each process uses its own log file).
3. Consider increasing `BATCH_SIZE` to 50 and monitoring the Dataverse service protection limits in the D365 admin centre.

---

## 12. Troubleshooting

### Authentication errors

| Error | Likely cause | Fix |
|---|---|---|
| `AADSTS700016` | CLIENT_ID not found | Verify the app registration exists in the correct tenant |
| `AADSTS7000215` | Invalid client secret | Regenerate the client secret |
| `401 Unauthorized` from Dataverse | Application user not created | Complete step 4.1 |
| `403 Forbidden` | Missing security role | Assign Omnichannel Agent role to the app user |

### Record creation errors

| Error | Likely cause | Fix |
|---|---|---|
| `400 — attribute name not valid` | Field name typo or wrong entity | Verify entity/field names in the [Dataverse table reference](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/about-entity-reference) |
| `Contact not created` | Email validation failure | Ensure `customer_email` is a valid email format |
| `Queue not found` | Queue name mismatch | Check exact queue name in D365 → Queues |
| `msdyn_ocliveworkitem not found` | Channel code invalid | Map channel to a valid `CHANNEL_MAP` value |

### Performance issues

| Symptom | Fix |
|---|---|
| Import is slow | Increase `BATCH_SIZE`, reduce sleep interval |
| Hitting 429 rate limits | Decrease `BATCH_SIZE`, increase sleep |
| Timeout errors | Reduce chunk size; check network connectivity |

---

## 13. Security Considerations

1. **Store secrets in a vault** — For production deployments, load `D365_CLIENT_SECRET` from Azure Key Vault rather than a `.env` file.
2. **Minimum-privilege app user** — Assign only the roles needed (Omnichannel Agent, not System Administrator).
3. **Rotate client secrets** — Set a maximum 12-month expiry and rotate before expiry.
4. **PII in transcripts** — Conversation transcripts may contain personal data. Ensure your data handling complies with your organisation's privacy policy and GDPR/regional regulations before loading historical data.
5. **Log file hygiene** — `load_transcripts.log` may contain email addresses. Restrict access to the log file and rotate it regularly.
6. **Network** — Run the script from inside your corporate network or a managed VM; avoid running it on personal devices.

---

## Related Resources

- [Dataverse Web API reference](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/overview)
- [msdyn_ocliveworkitem entity reference](https://learn.microsoft.com/en-us/dynamics365/customer-service/developer/reference/entities/msdyn_ocliveworkitem)
- [msdyn_conversationtranscript entity reference](https://learn.microsoft.com/en-us/dynamics365/customer-service/developer/reference/entities/msdyn_conversationtranscript)
- [Configure an application user in Dataverse](https://learn.microsoft.com/en-us/power-platform/admin/manage-application-users)
- [Dataverse service protection API limits](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/api-limits)

---

*Maintained by ACE DRI Team*
