# ACS Direct Routing → Teams Phone Extensibility (TPE) Migration Guide

**Version:** 1.1 | **Applies to:** Dynamics 365 Contact Center — Voice Channel  
**Replaces:** "Migration Guide: ACS telephony to Teams telephony" (manual PDF)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Key Differences](#2-key-differences)
3. [Required Roles](#3-required-roles)
4. [Phase 0 — Pre-Migration Planning](#4-phase-0--pre-migration-planning)
5. [Phase 1 — Teams Infrastructure Setup](#5-phase-1--teams-infrastructure-setup)
6. [Phase 2 — Bulk Number Migration (Script)](#6-phase-2--bulk-number-migration-script)
7. [Phase 3 — D365 Contact Center Sync](#7-phase-3--d365-contact-center-sync)
8. [Phase 4 — Cutover & Verification](#8-phase-4--cutover--verification)
9. [Phase 5 — Decommission ACS (Optional)](#9-phase-5--decommission-acs-optional)
10. [Rollback Procedure](#10-rollback-procedure)
11. [Troubleshooting](#11-troubleshooting)
12. [FAQ](#12-faq)

---

## 1. Overview

This guide walks through migrating from **Azure Communication Services (ACS) Direct Routing** to **Teams Phone Extensibility (TPE)** — either Teams Direct Routing or Teams Operator Connect — for Dynamics 365 Contact Center voice numbers.

The script `Invoke-TPEBulkNumberMigration.ps1` handles **hundreds of numbers in a single run** with dry-run, rollback, and validation modes.

### Migration modes

| Scenario | Description |
|---|---|
| **Full cutover** | All numbers migrate at once during a maintenance window |
| **Phased / hybrid** | Numbers migrate in batches; ACS and Teams coexist temporarily using an SBC alias FQDN |

### What the script automates

```
For each phone number in your CSV:
  A. Create or verify the Teams Resource Account (application instance)
  B. Stamp the Dynamics App ID + ACS Resource ID onto the account
  C. Sync the mapping into Agent Provisioning Service (APS)
  D. Assign the Direct Routing number to the resource account
  → Logs every step to a transaction log for rollback or audit
```

> [!IMPORTANT]
> **Step 3 (D365 Contact Center Sync) cannot be scripted via Teams PowerShell.** After the Teams-side migration, each number must be acknowledged in **Customer Service Admin Center → Channels → Phone Numbers → Manage → Advanced → Teams Phone System**. See [Phase 3](#7-phase-3--d365-contact-center-sync) for options.

---

## 2. Key Differences

| Area | ACS Direct Routing | Teams Direct Routing (TPE) |
|---|---|---|
| **Licensing** | Charges usage to ACS resource — no per-user Teams Phone license needed | Each user / resource account requires a **Teams Phone license** (E5 or add-on) |
| **Admin Portal** | Azure Portal → Communication Services resource | Teams Admin Center (TAC) + PowerShell |
| **SBC FQDN** | Registered in Azure Portal | Registered in TAC — **same FQDN cannot be used by both simultaneously** |
| **Resource Accounts** | Not required | Required — one per phone number / queue |
| **D365 Config** | Auto-discovered from ACS | Must sync via CSAC after Teams assignment |

---

## 3. Required Roles

You need **all** of the following before starting:

| Role | Where assigned | Required for |
|---|---|---|
| **Teams Administrator** *or* Teams Communications Administrator *or* Teams Voice Administrator | Microsoft 365 admin center | Running Teams PowerShell cmdlets, TAC configuration |
| **Global Administrator** *or* **User Administrator** | Azure Entra ID | Creating resource accounts (application instances). As of mid-2024, Teams admin roles alone are **insufficient** for `New-CsOnlineApplicationInstance` |
| **Azure Owner** *or* **Contributor** on ACS resource | Azure Portal | Removing/disabling ACS trunk settings |
| **SBC Administrator** | Your carrier / telecom partner | Updating SBC trunk config, certificates, firewall rules |
| **D365 System Administrator** | Dynamics 365 | Syncing phone numbers in Customer Service Admin Center |

> [!WARNING]
> If you encounter `New-CsOnlineApplicationInstance` permission errors, it is almost always a missing **User Administrator** or **Global Administrator** role in Entra ID — not a script issue.

---

## 4. Phase 0 — Pre-Migration Planning

Complete all of these before touching any production configuration.

### 4.1 Inventory your ACS numbers

Run this in Azure CLI or PowerShell to export all phone numbers on your ACS resource:

```powershell
# Azure PowerShell
$acsResource = Get-AzCommunicationService -ResourceGroupName "<rg>" -Name "<acs-name>"
# Or navigate in Azure Portal → Communication Services → Phone numbers
```

Document for each number:
- Phone number (E.164 format, e.g. `+12065551234`)
- Associated SBC FQDN
- Voice route patterns
- Any ACS bots / IVR applications dependent on this number

### 4.2 Prepare the migration CSV

Create a CSV file with one row per phone number. The script reads this file.

**Required columns:**

| Column | Description | Example |
|---|---|---|
| `ResourceUpn` | UPN for the Teams Resource Account. Must be in your tenant's domain. | `cc-queue-sales@contoso.onmicrosoft.com` |
| `DisplayName` | Friendly name for the resource account | `Contact Center - Sales Queue` |
| `PhoneNumber` | E.164 format — must start with `+` | `+12065551234` |

**Sample CSV (`numbers.csv`):**

```csv
ResourceUpn,DisplayName,PhoneNumber
cc-sales-01@contoso.onmicrosoft.com,CC Sales Queue 01,+12065550101
cc-sales-02@contoso.onmicrosoft.com,CC Sales Queue 02,+12065550102
cc-support-01@contoso.onmicrosoft.com,CC Support Queue 01,+14255550201
cc-support-02@contoso.onmicrosoft.com,CC Support Queue 02,+14255550202
```

> [!TIP]
> If you have Excel, use the provided `sample-numbers.csv` as a template. UPNs must be unique — a common convention is `cc-<queue>-<nn>@<tenant>.onmicrosoft.com`.

### 4.3 Count your Teams Phone licenses

```powershell
# Check available Teams Phone licenses in M365 Admin Center
# Or via Microsoft Graph:
Connect-MgGraph -Scopes "Directory.Read.All"
Get-MgSubscribedSku | Where { $_.SkuPartNumber -like "*MCOEV*" } |
  Select SkuPartNumber, @{n="Available";e={$_.PrepaidUnits.Enabled - $_.ConsumedUnits}}
```

You need **one Teams Phone license per resource account** (i.e., one per phone number). If short on licenses, request more before proceeding.

### 4.4 Plan your SBC transition

Decide on your cutover strategy:

| Strategy | When to use | How |
|---|---|---|
| **Full cutover** | Acceptable downtime window available | Remove SBC from ACS, then add to Teams. ~5 min propagation gap. |
| **Alias FQDN (parallel)** | Zero-downtime required | Add `sbc2.contoso.com` as an alias on your SBC; use it for Teams while `sbc.contoso.com` stays in ACS. Merge after cutover. Ensure cert covers both FQDNs. |

### 4.5 Verify SBC requirements

- **Certificate**: Must be from a [Microsoft-accepted CA](https://learn.microsoft.com/microsoftteams/direct-routing-plan#public-trusted-certificate-for-the-sbc). The SBC FQDN must be in the cert's SAN/CN.
- **Firewall**: SBC must reach `sip.pstnhub.microsoft.com` on port 5061 (SIP) and Microsoft media IP ranges.
- **SIP Options**: Teams requires periodic SIP OPTIONS ping from the SBC to verify trunk health.

### 4.6 Schedule the maintenance window

- Aim for lowest-traffic period (weekday late evening or weekend)
- Notify internal teams: IT, helpdesk, contact center operations
- Keep a rollback plan ready — the script includes `-Mode Rollback`

---

## 5. Phase 1 — Teams Infrastructure Setup

Do this **before** running the migration script. The Teams trunk must be ready to receive numbers.

### 5.1 Install Teams PowerShell module

```powershell
# Install (run once, as admin)
Install-Module MicrosoftTeams -Scope CurrentUser -Force
Import-Module MicrosoftTeams
Connect-MicrosoftTeams
```

### 5.2 Add your SBC as a Direct Routing trunk

**Option A — Teams Admin Center (TAC) UI:**

1. Go to **TAC → Voice → Direct Routing**
2. Click **Add** → enter your SBC FQDN (e.g., `sbc.contoso.com`)
3. Set SIP signaling port: `5061`
4. Enable/disable Media Bypass per your network design
5. Save

**Option B — PowerShell:**

```powershell
New-CsOnlinePSTNGateway `
  -Fqdn "sbc.contoso.com" `
  -SipSignalingPort 5061 `
  -MaxConcurrentSessions 100 `
  -Enabled $true `
  -MediaBypass $false
```

### 5.3 Configure voice routing

```powershell
# Create a PSTN usage
Set-CsOnlinePstnUsage -Usage @{Add="US-Voice"}

# Create a voice route for your number ranges
New-CsOnlineVoiceRoute `
  -Name "US-PSTN-Route" `
  -NumberPattern "^\+1" `
  -OnlinePstnGatewayList "sbc.contoso.com" `
  -Priority 1 `
  -OnlinePstnUsages "US-Voice"

# Create a voice routing policy
New-CsOnlineVoiceRoutingPolicy `
  -Identity "CC-Voice-Policy" `
  -OnlinePstnUsages "US-Voice"
```

### 5.4 Add phone numbers to Teams (bulk via CSV in TAC)

In **TAC → Voice → Phone numbers**:
1. Click **Add** or **Import**
2. Select **Direct Routing** as number type
3. Upload a CSV with your number range or paste individual numbers

### 5.5 Pilot test (strongly recommended)

Before migrating all numbers, test with one non-critical number:

```powershell
# Create a test resource account manually
New-CsOnlineApplicationInstance `
  -UserPrincipalName "cc-pilot-test@contoso.onmicrosoft.com" `
  -DisplayName "CC Pilot Test" `
  -ApplicationId "e404520c-564a-4e7c-9f33-ad12acc64a90"

# Assign a test number
Set-CsPhoneNumberAssignment `
  -Identity "cc-pilot-test@contoso.onmicrosoft.com" `
  -PhoneNumber "+10000000001" `
  -PhoneNumberType DirectRouting
```

Place a test call. Verify:
- Inbound call routes to the resource account ✓
- SIP OPTIONS shows trunk as healthy in TAC ✓
- Emergency address is recognized ✓

---

## 6. Phase 2 — Bulk Number Migration (Script)

### 6.1 Gather required parameters

Before running, you need:

| Parameter | Where to find it | Example |
|---|---|---|
| `$DynamicsAppId` | D365 CSAC → Channels → Phone Numbers → Advanced → copy "Application ID" | `e404520c-564a-4e7c-9f33-ad12acc64a90` |
| `$AcsResourceId` | Azure Portal → Communication Services → Properties → Resource ID (GUID portion) | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `$CsvPath` | Your prepared numbers.csv file | `C:\Migration\numbers.csv` |

### 6.2 Dry run first — always

```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
  -Mode Migrate `
  -CsvPath "C:\Migration\numbers.csv" `
  -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
  -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
  -DryRun
```

Review the console output. Dry run will:
- ✅ Validate CSV format and E.164 phone numbers
- ✅ Check for duplicates within the CSV
- ✅ Check which resource accounts already exist vs. need to be created
- ✅ Show exactly what would happen — no changes made

### 6.3 Run the migration

```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
  -Mode Migrate `
  -CsvPath "C:\Migration\numbers.csv" `
  -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
  -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
  -ThrottleMs 500
```

The script will:
1. Show a progress bar as it processes each number
2. Write a **transaction log** (`tpe_migration_txnlog.jsonl`) after each row
3. Continue past failures — failed rows are logged and skipped
4. Print a summary table when complete

**Example output:**
```
[1/150]  MIGRATE  cc-sales-01@contoso.onmicrosoft.com  +12065550101  → OK
[2/150]  MIGRATE  cc-sales-02@contoso.onmicrosoft.com  +12065550102  → OK
[3/150]  MIGRATE  cc-support-01@contoso.onmicrosoft.com  +14255550201  → FAILED: NumberAlreadyAssigned
...
========================================
 MIGRATION SUMMARY
========================================
 Succeeded : 148
 Failed    : 2
 Skipped   : 0
 Total     : 150
 Duration  : 00:06:22
 Txn log   : .\tpe_migration_txnlog.jsonl
 Results   : .\tpe_migration_results.csv
```

### 6.4 Validate the migration

After the run, use validate mode to confirm every number is correctly assigned:

```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
  -Mode Validate `
  -CsvPath "C:\Migration\numbers.csv" `
  -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
  -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

This reads back the Teams state and checks each number without making any changes.

### 6.5 Retry failed numbers only

```powershell
# The results CSV marks rows as OK or FAILED
# Re-run migrate using only the failed rows:
Import-Csv ".\tpe_migration_results.csv" |
  Where-Object { $_.Status -eq "FAILED" } |
  Select-Object ResourceUpn, DisplayName, PhoneNumber |
  Export-Csv "C:\Migration\numbers_retry.csv" -NoTypeInformation

.\Invoke-TPEBulkNumberMigration.ps1 `
  -Mode Migrate `
  -CsvPath "C:\Migration\numbers_retry.csv" `
  -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
  -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

---

## 7. Phase 3 — D365 Contact Center Sync

> [!IMPORTANT]
> This step is **required** for each number to become available for routing in D365 Contact Center. It cannot be done via the Teams PowerShell module — it requires the D365 admin portal or Dataverse API.

### 7.1 Manual sync in Customer Service Admin Center

For each number (or use a queue assignment to batch):

1. Open **Customer Service Admin Center (CSAC)**
2. Navigate to **Channels → Phone Numbers → Manage**
3. Click **Advanced** at the top right
4. Select the **Teams Phone System** tab
5. Your newly assigned Teams numbers should appear here
6. Select all numbers → **Sync** / **Add to D365**
7. Assign each number to its queue, workstream, or agent as required

### 7.2 Bulk sync via Dataverse API (alternative)

If you have many numbers, the `msdyn_phonenumber` entity in Dataverse can be queried and updated via the Web API. Contact your D365 developer or refer to the D365 Dataverse API documentation for bulk operations on `msdyn_ocphonenumber`.

> [!NOTE]
> A separate bulk sync script using the Dataverse Web API can be built if needed. Raise this requirement with your ACE engineer.

---

## 8. Phase 4 — Cutover & Verification

### 8.1 SBC cutover (full cutover scenario)

If using the same SBC FQDN for both ACS and Teams:

1. **Remove SBC from ACS** (Azure Portal → ACS resource → Direct Routing → delete trunk)
2. Wait 5–10 minutes for DNS propagation
3. **Verify Teams trunk is healthy**: TAC → Voice → Direct Routing → check SIP Options status shows ✅ Active

### 8.2 Test calls

- Place inbound test calls to 3–5 numbers across different queues
- Place outbound test calls
- Test emergency calling (dial test number, not `911`)
- Verify call recordings work (if applicable)
- Verify transcription works (if applicable)

### 8.3 Monitor for 24–48 hours

Watch:
- **TAC → Voice → Direct Routing**: SIP Options should stay green
- **D365 Contact Center**: Conversations should route correctly
- **SBC logs**: Check for any unexpected SIP errors (4xx, 5xx)

---

## 9. Phase 5 — Decommission ACS (Optional)

Once you are confident Teams is working:

1. **Azure Portal → Communication Services → Direct Routing**
   - Delete all SBC trunks
   - Delete all voice routing rules

2. **If numbers were ported to ACS**: Release or transfer them as needed

3. **ACS billing**: Removing numbers and trunks stops ACS usage billing

> [!NOTE]
> Keep the ACS resource itself if you use ACS for other features (chat, SMS, email). Only remove the Direct Routing / phone number configuration.

---

## 10. Rollback Procedure

If migration needs to be undone:

### Rollback via script (removes phone number assignments)

```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
  -Mode Rollback `
  -CsvPath "C:\Migration\numbers.csv" `
  -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
  -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
  -TxnLogPath ".\tpe_migration_txnlog.jsonl"
```

The rollback mode:
- Reads the transaction log
- Only rolls back rows that were **successfully migrated by this script** (`AssignedNow = true`)
- Removes the phone number assignment from Teams
- Does **not** re-add the SBC to ACS — that must be done manually in Azure Portal

### Re-enable ACS Direct Routing (manual)

1. Azure Portal → ACS resource → Direct Routing
2. Re-add your SBC trunk
3. Re-add voice routing rules
4. Verify ACS is handling calls again before notifying users

---

## 11. Troubleshooting

### Script errors

| Error message | Cause | Fix |
|---|---|---|
| `Insufficient privileges to complete the operation` on `New-CsOnlineApplicationInstance` | Missing **User Administrator** or **Global Administrator** Entra ID role | Assign the role in M365 Admin Center → Active users → the account running the script |
| `The phone number is already assigned` | Number already assigned to another resource account | Run Validate mode to see where it's assigned; use `Remove-CsPhoneNumberAssignment` first |
| `User not found` on `Set-CsOnlineApplicationInstance` | Resource account not yet replicated to all services | Wait 30–60 seconds after creation and retry; script retries automatically |
| `NumberAlreadyAssigned` | Number already exists on Teams | Check in TAC → Phone Numbers |
| Non-E.164 phone number validation failure | Number format incorrect | Ensure all numbers in CSV start with `+` and contain 8–15 digits |

### Teams trunk not showing healthy

- Check SBC certificate is valid and FQDN matches
- Verify firewall allows SBC to reach `sip.pstnhub.microsoft.com:5061`
- Check SBC is sending SIP OPTIONS at the configured interval

### D365 Contact Center not routing calls

- Verify Step 3 (CSAC sync) was completed
- Check the queue/workstream the number is assigned to in CSAC
- Verify the D365 voice channel is enabled and the workstream is active

---

## 12. FAQ

**Q: Can I migrate numbers in batches rather than all at once?**  
A: Yes. Split your CSV into batches and run the script multiple times. The transaction log accumulates entries. Numbers already migrated will be detected by Teams and skipped gracefully.

**Q: Will there be downtime during migration?**  
A: For a full cutover (same SBC FQDN), there is typically a 5–10 minute window when the SBC is being moved from ACS to Teams. For an alias/parallel approach, downtime is near zero.

**Q: What if a number fails halfway through?**  
A: The script logs the exact step that failed in the transaction log (`tpe_migration_txnlog.jsonl`). Fix the issue and re-run with the retry CSV (see Section 6.5). No need to re-run successful numbers.

**Q: Do I need a Teams Phone license for every phone number?**  
A: Yes — one Teams Phone license (E5 or add-on) per resource account, which means one per phone number configured in TPE.

**Q: Can I use Operator Connect instead of Direct Routing?**  
A: Yes. The D365/Teams side is the same (resource accounts, CSAC sync). The difference is that Operator Connect is configured in **TAC → Voice → Operators** rather than via Direct Routing trunks. The PowerShell steps for `New-CsOnlineApplicationInstance`, `Set-CsOnlineApplicationInstance`, and `Set-CsPhoneNumberAssignment` remain the same.

**Q: How do I find my Dynamics App ID?**  
A: Open **Customer Service Admin Center → Channels → Phone Numbers → Manage → Advanced**. The Application ID is displayed on the Teams Phone System tab.

**Q: The SBC FQDN is already in use by Teams and I can't add it to ACS — or vice versa.**  
A: This is by design. Microsoft prevents the same FQDN from being registered on both platforms simultaneously. Use the alias FQDN approach (Section 4.4) for parallel coexistence.
