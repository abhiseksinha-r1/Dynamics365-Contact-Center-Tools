# ACS Direct Routing → Teams Phone Extensibility (TPE) Migration Tool

Bulk-migrate, validate, and rollback phone numbers for Dynamics 365 Contact Center.

## Quick Start

### 1. Install prerequisites
```powershell
Install-Module MicrosoftTeams -Scope CurrentUser -Force
```

### 2. Prepare your CSV
Edit `sample-numbers.csv` (or create your own) with one row per phone number:
```
ResourceUpn,DisplayName,PhoneNumber
cc-sales@contoso.com,Sales Main Line,+12065551001
cc-support@contoso.com,Support,+12065551002
```
All phone numbers **must be in E.164 format** (e.g. `+12065551234`).

### 3. Find your IDs
| Value | Where to find it |
|---|---|
| `DynamicsAppId` | CSAC → Channels → Phone Numbers → Manage → Advanced → Teams Phone System |
| `AcsResourceId` | Azure Portal → Communication Services → Properties → Resource ID (GUID only) |

The standard D365 Contact Center `DynamicsAppId` is `e404520c-564a-4e7c-9f33-ad12acc64a90`.

### 4. Dry run first
Always preview before making changes:
```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
    -Mode Migrate `
    -CsvPath .\sample-numbers.csv `
    -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
    -AcsResourceId "YOUR-ACS-RESOURCE-GUID" `
    -DryRun
```

### 5. Run the migration
```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
    -Mode Migrate `
    -CsvPath .\numbers.csv `
    -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
    -AcsResourceId "YOUR-ACS-RESOURCE-GUID" `
    -ThrottleMs 500 `
    -AutoRollbackOnFailure
```

### 6. Validate results
```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
    -Mode Validate `
    -CsvPath .\numbers.csv `
    -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
    -AcsResourceId "YOUR-ACS-RESOURCE-GUID"
```

### 7. Sync into D365 Contact Center (manual step)
After the script completes, open **Customer Service Admin Center**:
> Channels → Phone Numbers → Manage → Advanced → Teams Phone System

Select all newly migrated numbers and click **Sync**. This step cannot be automated.

### 8. Rollback if needed
```powershell
.\Invoke-TPEBulkNumberMigration.ps1 `
    -Mode Rollback `
    -CsvPath .\numbers.csv `
    -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
    -AcsResourceId "YOUR-ACS-RESOURCE-GUID" `
    -TxnLogPath .\tpe_migration_txnlog.jsonl
```

---

## Output Files

| File | Description |
|---|---|
| `tpe_migration_txnlog.jsonl` | Append-only transaction log (one JSON per row, per attempt). Used for resume and rollback. |
| `tpe_migration_results.csv` | Final status per row: Succeeded / Failed / Skipped |
| `tpe_migration_results_validation.csv` | Validation report: PASS / WARN / FAIL |

---

## Key Parameters

| Parameter | Default | Description |
|---|---|---|
| `-Mode` | required | `Migrate` \| `Validate` \| `Rollback` |
| `-CsvPath` | required | Input CSV file path |
| `-DynamicsAppId` | required | D365 CC Application ID |
| `-AcsResourceId` | required | ACS Resource GUID |
| `-DryRun` | off | Preview only — no changes |
| `-AutoRollbackOnFailure` | off | Auto-remove number if post-assignment step fails |
| `-ThrottleMs` | 200 | Delay (ms) between rows. Use 500–1000 for large batches |
| `-MaxRetries` | 3 | Retry attempts for transient errors |
| `-TxnLogPath` | `.\tpe_migration_txnlog.jsonl` | Transaction log path |
| `-OutputPath` | `.\tpe_migration_results.csv` | Results CSV path |

---

## Resume Support

If a migration run is interrupted, simply re-run the same command. The script reads the transaction log and **automatically skips rows that already succeeded**, processing only the remaining rows.

---

## Required Permissions

- **Teams Administrator** or **Teams Communications Administrator** — for Teams PS cmdlets
- **User Administrator** or **Global Administrator** in Entra ID — for creating new resource accounts (`New-CsOnlineApplicationInstance`)

> If resource accounts already exist in your tenant (pre-created by your admin team), Teams Administrator is sufficient.

---

## Full Documentation

See [`ACS-to-TPE-Migration-Guide.md`](./ACS-to-TPE-Migration-Guide.md) for:
- Complete phase-by-phase migration planning
- SBC alias / zero-downtime parallel migration strategy
- Troubleshooting guide for common errors
- CSAC sync walkthrough with screenshots references
- Decommission checklist
