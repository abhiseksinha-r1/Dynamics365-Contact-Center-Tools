"""
load_transcripts.py
===================
Template script for loading conversation transcript data from a third-party
system into Dynamics 365 Contact Center via the Dataverse Web API.

PREREQUISITES
-------------
  pip install msal requests python-dotenv

CONFIGURATION
-------------
Copy .env.example to .env and fill in the values:
  D365_TENANT_ID       - Azure AD tenant ID
  D365_CLIENT_ID       - App registration client ID
  D365_CLIENT_SECRET   - App registration client secret
  D365_ENVIRONMENT_URL - https://<org>.crm.dynamics.com

USAGE
-----
  python load_transcripts.py --source transcripts.json [--dry-run] [--log-level DEBUG]

See docs/Dynamics-365-CE/Tech-Topics/Contact-Center/Load-Transcripts-to-D365-Contact-Center.md
for full documentation.
"""

import argparse
import base64
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msal
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Step 1 — Configuration
# ---------------------------------------------------------------------------

load_dotenv()

# --- D365 / Azure AD settings (from environment) ---
TENANT_ID: str = os.getenv("D365_TENANT_ID", "")
CLIENT_ID: str = os.getenv("D365_CLIENT_ID", "")
CLIENT_SECRET: str = os.getenv("D365_CLIENT_SECRET", "")
ENVIRONMENT_URL: str = os.getenv("D365_ENVIRONMENT_URL", "").rstrip("/")

# --- Dataverse Web API ---
API_VERSION: str = "9.2"
API_BASE: str = f"{ENVIRONMENT_URL}/api/data/v{API_VERSION}"

# --- Retry settings ---
MAX_RETRIES: int = 3
RETRY_BACKOFF_BASE: float = 2.0   # seconds

# --- Batch size for bulk processing ---
BATCH_SIZE: int = 20

# --- Channel code mapping (third-party name → D365 option-set value) ---
# Update this map to match your source system's channel names.
CHANNEL_MAP: dict[str, int] = {
    "chat":       192350000,
    "sms":        192350001,
    "facebook":   192350002,
    "whatsapp":   192350003,
    "custom":     192350005,
    "voice":      192350008,
    "email":      192350012,
    "teams":      192350013,
}

# ---------------------------------------------------------------------------
# Step 2 — Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("load_transcripts.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step 3 — Authentication (MSAL client-credentials flow)
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Acquire a Dataverse access token using the client-credentials flow."""
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    scope = [f"{ENVIRONMENT_URL}/.default"]

    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=authority,
        client_credential=CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(scopes=scope)

    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire token: {result.get('error_description', result)}"
        )

    log.debug("Access token acquired successfully.")
    return result["access_token"]

# ---------------------------------------------------------------------------
# Step 4 — Dataverse HTTP helpers
# ---------------------------------------------------------------------------

class DataverseClient:
    """Thin wrapper around the Dataverse Web API with retry logic."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "Prefer": "return=representation",
            }
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Execute an HTTP request with exponential-backoff retries."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", RETRY_BACKOFF_BASE ** attempt))
                    log.warning("Rate-limited. Waiting %ds before retry %d/%d.", retry_after, attempt, MAX_RETRIES)
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp
            except requests.HTTPError as exc:
                log.error("HTTP %s on attempt %d/%d: %s", exc.response.status_code, attempt, MAX_RETRIES, exc.response.text[:500])
                if attempt == MAX_RETRIES or exc.response.status_code in (400, 401, 403):
                    raise
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
            except requests.RequestException as exc:
                log.error("Request error on attempt %d/%d: %s", attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
        raise RuntimeError("Exhausted retries.")

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE}/{path}"
        resp = self._request("GET", url, params=params)
        return resp.json()

    def post(self, path: str, payload: dict) -> dict:
        url = f"{API_BASE}/{path}"
        resp = self._request("POST", url, json=payload)
        return resp.json() if resp.content else {}

    def patch(self, path: str, payload: dict) -> None:
        url = f"{API_BASE}/{path}"
        self._request("PATCH", url, json=payload)

    def upsert(self, entity: str, alternate_key: str, alternate_value: str, payload: dict) -> dict:
        """Upsert a record using an alternate key (prevent duplicates)."""
        path = f"{entity}({alternate_key}='{alternate_value}')"
        url = f"{API_BASE}/{path}"
        self.session.headers.update({"If-None-Match": "*", "If-Match": ""})
        resp = self._request("PATCH", url, json=payload)
        # Re-read to return the created/updated record
        self.session.headers.pop("If-None-Match", None)
        self.session.headers.pop("If-Match", None)
        return self.get(path)

    def find_by_field(self, entity: str, field: str, value: str) -> str | None:
        """Return the GUID of the first record matching field=value, or None."""
        result = self.get(entity, params={"$filter": f"{field} eq '{value}'", "$select": f"{entity[:-1]}id", "$top": "1"})
        records = result.get("value", [])
        return records[0][f"{entity[:-1]}id"] if records else None

# ---------------------------------------------------------------------------
# Step 5 — Source data loading  (CUSTOMIZE for your third-party system)
# ---------------------------------------------------------------------------

def load_source_data(source_path: str) -> list[dict]:
    """
    Load transcript records from the third-party export file.

    Supported formats:
      - JSON  (list of objects or {conversations: [...]})
      - CSV   (one row per conversation, with a 'messages' JSON column)

    Each returned dict must contain at minimum:
      external_id       Unique ID in the source system (used as idempotency key)
      channel           Chat channel name (see CHANNEL_MAP)
      customer_email    Customer email address (used to link/create a Contact)
      customer_name     Customer display name
      start_time        ISO-8601 datetime string (UTC preferred)
      end_time          ISO-8601 datetime string
      subject           Short conversation subject/title
      messages          List of message objects (see _normalize_messages below)

    OPTIONAL fields:
      queue_name        D365 queue name (leave blank to skip queue assignment)
      agent_email       Agent's UPN in Azure AD (leave blank to skip agent link)
      language          ISO 639-1 code, e.g. "en"
      tags              List of strings for labelling
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        records = raw if isinstance(raw, list) else raw.get("conversations", raw.get("records", []))
    elif suffix == ".csv":
        records = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if "messages" in row and isinstance(row["messages"], str):
                    row["messages"] = json.loads(row["messages"])
                records.append(row)
    else:
        raise ValueError(f"Unsupported file format: {suffix}. Use .json or .csv")

    log.info("Loaded %d records from %s.", len(records), source_path)
    return records


def _normalize_messages(raw_messages: list[dict]) -> list[dict]:
    """
    Normalize third-party message objects into the native D365 Contact Center
    transcript JSON schema — a flat array stored base64-encoded in an annotation.

    Native D365 transcript message schema (per Microsoft docs):
    [
      {
        "id": "<unix-ms timestamp or guid>",
        "content": "<message text>",
        "contentType": "text",
        "createdDateTime": "<ISO-8601 with offset>",
        "created": "<ISO-8601 UTC>",
        "from": {
          "user": {
            "displayName": "<name>",
            "id": "<user-id>"
          }
        },
        "likes": [],
        "attachments": [],
        "tags": "public",        // "public" = agent→customer, "private" = agent→agent
        "isControlMessage": false
      }
    ]

    Reference: https://learn.microsoft.com/dynamics365/customer-service/develop/download-transcripts-bulk

    CUSTOMIZE this function to map your source system's message schema.
    """
    normalized = []
    for msg in raw_messages:
        role = msg.get("role") or msg.get("from", {}).get("role", "customer")
        sender_id = msg.get("sender_id") or msg.get("from", {}).get("id", "unknown")
        sender_name = msg.get("sender_name") or msg.get("from", {}).get("displayName", "Unknown")
        timestamp = msg.get("timestamp") or msg.get("created_at") or datetime.now(timezone.utc).isoformat()
        text = msg.get("text") or msg.get("content", {}).get("value", "") if isinstance(msg.get("content"), dict) else msg.get("content", "")

        # D365 uses "public" tag for agent→customer messages, "private" for agent→agent
        tags = "public" if role == "agent" else ""

        normalized.append(
            {
                "id": msg.get("id") or str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "content": text,
                "contentType": "text",
                "createdDateTime": timestamp,
                "created": timestamp,
                "from": {
                    "user": {
                        "displayName": sender_name,
                        "id": sender_id,
                    }
                },
                "likes": [],
                "attachments": [],
                "tags": tags,
                "isControlMessage": False,
            }
        )
    return normalized

# ---------------------------------------------------------------------------
# Step 6 — D365 entity operations
# ---------------------------------------------------------------------------

def resolve_contact(client: DataverseClient, email: str, name: str, dry_run: bool) -> str | None:
    """
    Return the contactid for the given email, creating the Contact if not found.
    Returns None in dry-run mode.
    """
    existing = client.find_by_field("contacts", "emailaddress1", email)
    if existing:
        log.debug("Found existing contact %s for %s.", existing, email)
        return existing

    if dry_run:
        log.info("[DRY-RUN] Would create Contact: %s <%s>", name, email)
        return None

    first, *rest = name.split(" ", 1)
    payload = {
        "firstname": first,
        "lastname": " ".join(rest) or "Unknown",
        "emailaddress1": email,
    }
    result = client.post("contacts", payload)
    contact_id = result.get("contactid")
    log.info("Created Contact %s for %s <%s>.", contact_id, name, email)
    return contact_id


def resolve_queue(client: DataverseClient, queue_name: str) -> str | None:
    """Return the queueid for queue_name, or None if not found."""
    if not queue_name:
        return None
    return client.find_by_field("queues", "name", queue_name)


def resolve_systemuser(client: DataverseClient, agent_upn: str) -> str | None:
    """Return the systemuserid for agent_upn, or None if not found."""
    if not agent_upn:
        return None
    return client.find_by_field("systemusers", "internalemailaddress", agent_upn)


def create_conversation(
    client: DataverseClient,
    record: dict,
    contact_id: str | None,
    queue_id: str | None,
    agent_id: str | None,
    dry_run: bool,
) -> str | None:
    """
    Create an msdyn_ocliveworkitem shell record representing a closed,
    imported conversation from a third-party system.

    Because this is a third-party import there is no existing work item — we
    create the shell from scratch.  All downstream records (msdyn_ocsession,
    msdyn_sessionparticipant, msdyn_transcript, annotation) depend on this ID.

    Status for a closed/resolved imported conversation:
      statecode  = 1  (Inactive / Completed activity)
      statuscode = 4  (Closed — NOT 5 which is Wrap-up)

    Returns the msdyn_ocliveworkitemid, or None in dry-run mode.
    """
    channel_code = CHANNEL_MAP.get(record.get("channel", "").lower(), CHANNEL_MAP["chat"])
    start_time = record.get("start_time")
    end_time = record.get("end_time")

    # Calculate duration in seconds for msdyn_activeduration
    duration_seconds: int | None = None
    if start_time and end_time:
        try:
            from datetime import datetime, timezone
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            t0 = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            duration_seconds = int((t1 - t0).total_seconds())
        except Exception:
            pass

    # subject is ApplicationRequired on the base Activity entity
    subject = record.get("subject") or f"Imported conversation – {record.get('external_id', '')}"

    payload: dict[str, Any] = {
        # Activity (base entity) fields
        "subject": subject,
        "actualstart": start_time,
        "actualend": end_time,
        # OC-specific fields
        "msdyn_title": subject,
        "msdyn_channel": channel_code,
        "msdyn_createdon": start_time,
        "msdyn_closedon": end_time,
        "msdyn_isoutbound": False,
        # Status: statecode=1 (Inactive), statuscode=4 (Closed)
        "statecode": 1,
        "statuscode": 4,
        # Historical import date
        "overriddencreatedon": start_time,
    }

    if duration_seconds is not None:
        payload["msdyn_activeduration"] = duration_seconds

    if contact_id:
        payload["customerid_contact@odata.bind"] = f"/contacts({contact_id})"
    if queue_id:
        payload["msdyn_queueid@odata.bind"] = f"/queues({queue_id})"
    if agent_id:
        payload["ownerid@odata.bind"] = f"/systemusers({agent_id})"

    # Idempotency — check if already imported by external_id
    external_id = record.get("external_id", "")
    if external_id:
        existing = client.find_by_field(
            "msdyn_ocliveworkitems",
            "msdyn_thirdpartyconversationid",
            external_id,
        )
        if existing:
            log.info("Conversation already imported (external_id=%s → %s). Skipping.", external_id, existing)
            return existing
        payload["msdyn_thirdpartyconversationid"] = external_id

    if dry_run:
        log.info("[DRY-RUN] Would create msdyn_ocliveworkitem: %s", json.dumps(payload, indent=2))
        return None

    result = client.post("msdyn_ocliveworkitems", payload)
    work_item_id = result.get("msdyn_ocliveworkitemid")
    log.info("Created msdyn_ocliveworkitem %s (external_id=%s).", work_item_id, external_id)
    return work_item_id


def create_session(
    client: DataverseClient,
    record: dict,
    work_item_id: str,
    agent_id: str | None,
    dry_run: bool,
) -> str | None:
    """
    Create an msdyn_ocsession shell record representing the agent's session
    within the imported conversation.

    msdyn_ocsession is the child entity of msdyn_ocliveworkitem that tracks
    agent-level interaction. Without it, handle-time and session-level metrics
    (Avg. handle time, Total sessions) will be missing from historical analytics.

    Returns the msdyn_ocsessionid, or None in dry-run mode.
    """
    channel_code = CHANNEL_MAP.get(record.get("channel", "").lower(), CHANNEL_MAP["chat"])

    payload: dict[str, Any] = {
        "msdyn_name": record.get("subject") or f"Session – {record.get('external_id', '')}",
        "msdyn_sessioncreatedon": record.get("start_time"),
        "msdyn_sessionclosedtime": record.get("end_time"),
        "msdyn_channel": channel_code,
        "msdyn_liveworkitemid@odata.bind": f"/msdyn_ocliveworkitems({work_item_id})",
        # statecode=1 (Inactive/closed session)
        "statecode": 1,
        "statuscode": 2,
    }

    if agent_id:
        payload["msdyn_agentid@odata.bind"] = f"/systemusers({agent_id})"

    if dry_run:
        log.info("[DRY-RUN] Would create msdyn_ocsession for work_item %s.", work_item_id)
        return None

    result = client.post("msdyn_ocsessions", payload)
    session_id = result.get("msdyn_ocsessionid")
    log.info("Created msdyn_ocsession %s.", session_id)
    return session_id


def create_session_participant(
    client: DataverseClient,
    record: dict,
    session_id: str,
    agent_id: str | None,
    dry_run: bool,
) -> str | None:
    """
    Create an msdyn_sessionparticipant record linking the agent to the session.

    msdyn_sessionparticipant drives per-agent handle-time metrics
    (msdyn_activetime, msdyn_activewrapuptime). Without it, agent-level
    analytics will be incomplete.

    Returns the msdyn_sessionparticipantid, or None in dry-run mode.
    """
    if not agent_id:
        log.debug("No agent_id — skipping msdyn_sessionparticipant creation.")
        return None

    # Calculate active time in seconds from conversation duration
    active_time_seconds = 0
    start_time = record.get("start_time")
    end_time = record.get("end_time")
    if start_time and end_time:
        try:
            t0 = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            active_time_seconds = int((t1 - t0).total_seconds())
        except Exception:
            pass

    payload: dict[str, Any] = {
        "msdyn_joinedon": record.get("start_time"),
        "msdyn_activetime": active_time_seconds,
        "msdyn_activewrapuptime": 0,
        "msdyn_ocsessionid@odata.bind": f"/msdyn_ocsessions({session_id})",
        "msdyn_agentid@odata.bind": f"/systemusers({agent_id})",
    }

    if dry_run:
        log.info("[DRY-RUN] Would create msdyn_sessionparticipant for session %s.", session_id)
        return None

    result = client.post("msdyn_sessionparticipants", payload)
    participant_id = result.get("msdyn_sessionparticipantid")
    log.info("Created msdyn_sessionparticipant %s.", participant_id)
    return participant_id


def create_transcript_record(
    client: DataverseClient,
    record: dict,
    work_item_id: str,
    dry_run: bool,
) -> str | None:
    """
    Create an msdyn_transcript record (metadata) linked to the work item.

    The msdyn_transcript entity stores transcript metadata only.
    The actual transcript content is stored separately in the annotations table
    as a base64-encoded JSON blob (see create_transcript_annotation below).

    Returns the msdyn_transcriptid, or None in dry-run mode.
    """
    payload: dict[str, Any] = {
        "msdyn_name": record.get("subject", "Imported Transcript"),
        "overriddencreatedon": record.get("start_time"),
        "msdyn_ocliveworkitemid@odata.bind": f"/msdyn_ocliveworkitems({work_item_id})",
    }

    if dry_run:
        log.info("[DRY-RUN] Would create msdyn_transcript for work_item %s.", work_item_id)
        return None

    result = client.post("msdyn_transcripts", payload)
    transcript_id = result.get("msdyn_transcriptid")
    log.info("Created msdyn_transcript %s.", transcript_id)
    return transcript_id


def create_transcript_annotation(
    client: DataverseClient,
    record: dict,
    transcript_id: str,
    dry_run: bool,
) -> str | None:
    """
    Create an annotation (Note) record that stores the transcript content.

    Per Microsoft documentation, D365 Contact Center transcripts are stored as
    base64-encoded JSON in the annotations table, linked via:
      objecttypecode = 'msdyn_transcript'
      objectid       = msdyn_transcriptid GUID

    The annotation documentbody field holds the base64-encoded JSON array of
    messages. Retrieve transcripts via:
      GET /api/data/v9.1/annotations?$filter=objecttypecode eq 'msdyn_transcript'

    Reference: https://learn.microsoft.com/dynamics365/customer-service/develop/download-transcripts-bulk
    """
    messages = _normalize_messages(record.get("messages", []))
    transcript_json = json.dumps(messages, ensure_ascii=False)

    encoded_content = base64.b64encode(transcript_json.encode("utf-8")).decode("ascii")

    payload: dict[str, Any] = {
        "subject": record.get("subject", "Imported Transcript"),
        "documentbody": encoded_content,
        "filename": "transcript.json",
        "mimetype": "application/json",
        "isdocument": True,
        "objecttypecode": "msdyn_transcript",
        "objectid_msdyn_transcript@odata.bind": f"/msdyn_transcripts({transcript_id})",
    }

    if dry_run:
        log.info("[DRY-RUN] Would create annotation (transcript content) for msdyn_transcript %s.", transcript_id)
        return None

    result = client.post("annotations", payload)
    annotation_id = result.get("annotationid")
    log.info("Created annotation %s (transcript content, %d messages).", annotation_id, len(messages))
    return annotation_id

# ---------------------------------------------------------------------------
# Step 7 — Main processing loop
# ---------------------------------------------------------------------------

def process_records(records: list[dict], client: DataverseClient, dry_run: bool) -> None:
    """Iterate over source records and load each one into D365 Contact Center."""
    total = len(records)
    success_count = 0
    skip_count = 0
    error_count = 0
    errors: list[dict] = []

    for idx, record in enumerate(records, start=1):
        external_id = record.get("external_id", f"row-{idx}")
        log.info("--- Processing record %d/%d  (external_id=%s) ---", idx, total, external_id)

        try:
            # Validate required fields
            _validate_record(record, idx)

            # Step A: Resolve/create Contact
            contact_id = resolve_contact(
                client,
                record.get("customer_email", ""),
                record.get("customer_name", "Unknown"),
                dry_run,
            )

            # Step B: Resolve Queue (optional)
            queue_id = resolve_queue(client, record.get("queue_name", ""))

            # Step C: Resolve Agent/SystemUser (optional)
            agent_id = resolve_systemuser(client, record.get("agent_email", ""))

            # Step D: Create shell Conversation (msdyn_ocliveworkitem)
            # Since this is a third-party import, no work item exists yet — we
            # create it from scratch as a closed/inactive shell record.
            work_item_id = create_conversation(client, record, contact_id, queue_id, agent_id, dry_run)

            if work_item_id is None and not dry_run:
                # Already imported (idempotency check returned existing); skip.
                skip_count += 1
                continue

            # Step E: Create shell Session (msdyn_ocsession)
            # Required for handle-time and session-level historical analytics.
            session_id = create_session(client, record, work_item_id or "<new>", agent_id, dry_run)

            # Step F: Create Session Participant (msdyn_sessionparticipant)
            # Links the agent to the session; drives per-agent handle-time metrics.
            if session_id or dry_run:
                create_session_participant(client, record, session_id or "<new>", agent_id, dry_run)

            # Step G: Create Transcript metadata record (msdyn_transcript)
            transcript_id = create_transcript_record(client, record, work_item_id or "<new>", dry_run)

            # Step H: Create Annotation with base64-encoded transcript JSON content
            if transcript_id or dry_run:
                create_transcript_annotation(client, record, transcript_id or "<new>", dry_run)

            success_count += 1

        except Exception as exc:  # noqa: BLE001
            log.error("Failed to import record %s: %s", external_id, exc, exc_info=True)
            error_count += 1
            errors.append({"external_id": external_id, "error": str(exc)})

        # Throttle to avoid hitting API rate limits on large batches
        if idx % BATCH_SIZE == 0:
            log.info("Processed %d/%d records. Pausing 2s to avoid throttling…", idx, total)
            time.sleep(2)

    # --- Summary ---
    log.info("=" * 60)
    log.info("Import complete. Total: %d | Success: %d | Skipped: %d | Errors: %d",
             total, success_count, skip_count, error_count)

    if errors:
        error_file = "load_transcripts_errors.json"
        with open(error_file, "w", encoding="utf-8") as fh:
            json.dump(errors, fh, indent=2)
        log.warning("Error details written to %s", error_file)


def _validate_record(record: dict, idx: int) -> None:
    """Raise ValueError for records missing required fields."""
    required = ["external_id", "channel", "customer_email", "start_time", "end_time"]
    missing = [f for f in required if not record.get(f)]
    if missing:
        raise ValueError(f"Record {idx} is missing required fields: {missing}")

# ---------------------------------------------------------------------------
# Step 8 — CLI entry point
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load transcript data from a third-party system into D365 Contact Center."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the source data file (.json or .csv).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and log what would be imported without writing to D365.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.getLogger().setLevel(args.log_level)

    if args.dry_run:
        log.info("*** DRY-RUN mode — no records will be written to D365 ***")

    # Validate config
    missing_cfg = [k for k, v in {
        "D365_TENANT_ID": TENANT_ID,
        "D365_CLIENT_ID": CLIENT_ID,
        "D365_CLIENT_SECRET": CLIENT_SECRET,
        "D365_ENVIRONMENT_URL": ENVIRONMENT_URL,
    }.items() if not v]
    if missing_cfg:
        log.error("Missing required environment variables: %s", missing_cfg)
        sys.exit(1)

    # Step 1: Authenticate
    log.info("Authenticating with Dataverse (%s)…", ENVIRONMENT_URL)
    token = get_access_token()

    # Step 2: Build client
    client = DataverseClient(token)

    # Step 3: Load source data
    records = load_source_data(args.source)

    # Step 4: Process
    process_records(records, client, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
