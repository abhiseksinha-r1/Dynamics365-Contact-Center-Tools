<#
.SYNOPSIS
    Bulk migrate / validate / rollback ACS Direct Routing phone numbers to Teams
    Phone Extensibility (TPE) for Dynamics 365 Contact Center.

.DESCRIPTION
    Modes:
      -Mode Migrate   : For each row in the CSV — create/verify resource account,
                        stamp ApplicationId + AcsResourceId, sync to APS, assign number.
      -Mode Validate  : Read-only check that every number in the CSV is correctly
                        assigned in Teams. Produces a validation report. No changes.
      -Mode Rollback  : Read the transaction log produced by -Mode Migrate and
                        remove phone number assignments for rows that were assigned
                        by this script.

    Key features:
      - Processes hundreds of numbers without manual intervention
      - Dry run mode (-DryRun): shows what would happen, makes no changes
      - Progress bar and per-row status as it runs
      - Transaction log (JSONL) written after each row for audit and rollback
      - Results CSV written at completion
      - Automatic retry with exponential backoff on transient errors
      - Auto-rollback on failure (-AutoRollbackOnFailure)
      - Configurable throttle delay between rows (-ThrottleMs)
      - Resumes safely: already-succeeded rows are skipped on re-run if txn log exists
      - E.164 format validation and duplicate detection before starting

.PARAMETER Mode
    Migrate | Validate | Rollback

.PARAMETER CsvPath
    Path to the input CSV file. Required columns: ResourceUpn, DisplayName, PhoneNumber

.PARAMETER DynamicsAppId
    The Dynamics / Contact Center Application ID. Find this in CSAC → Channels →
    Phone Numbers → Manage → Advanced → Teams Phone System tab.
    Default (standard D365 CC app): e404520c-564a-4e7c-9f33-ad12acc64a90

.PARAMETER AcsResourceId
    The ACS Resource ID GUID. Find this in Azure Portal → Communication Services
    → Properties → Resource ID (use only the GUID portion).

.PARAMETER PhoneNumberType
    DirectRouting (default) or OperatorConnect

.PARAMETER TxnLogPath
    Path to the transaction log JSONL file. Defaults to .\tpe_migration_txnlog.jsonl

.PARAMETER OutputPath
    Path for the results CSV. Defaults to .\tpe_migration_results.csv

.PARAMETER DryRun
    If set, validates and reports what would happen but makes NO changes.

.PARAMETER AutoRollbackOnFailure
    If set, automatically rolls back the phone number assignment for a row
    if any step after assignment fails.

.PARAMETER ThrottleMs
    Milliseconds to wait between processing each row. Use 500-1000 for large
    batches to avoid Teams API rate limits. Default: 200

.PARAMETER MaxRetries
    Number of retry attempts for transient errors. Default: 3

.PARAMETER InitialBackoffSeconds
    Starting backoff seconds for retries. Doubles each attempt. Default: 2

.EXAMPLE
    # Dry run — preview without making changes
    .\Invoke-TPEBulkNumberMigration.ps1 -Mode Migrate -CsvPath .\numbers.csv `
      -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
      -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" -DryRun

.EXAMPLE
    # Full migration run
    .\Invoke-TPEBulkNumberMigration.ps1 -Mode Migrate -CsvPath .\numbers.csv `
      -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
      -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" -ThrottleMs 500

.EXAMPLE
    # Validate all numbers are correctly assigned
    .\Invoke-TPEBulkNumberMigration.ps1 -Mode Validate -CsvPath .\numbers.csv `
      -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
      -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

.EXAMPLE
    # Rollback using transaction log
    .\Invoke-TPEBulkNumberMigration.ps1 -Mode Rollback -CsvPath .\numbers.csv `
      -DynamicsAppId "e404520c-564a-4e7c-9f33-ad12acc64a90" `
      -AcsResourceId "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
      -TxnLogPath .\tpe_migration_txnlog.jsonl

.NOTES
    Prerequisites:
      Install-Module MicrosoftTeams -Scope CurrentUser

    Required permissions:
      - Teams Administrator OR Teams Communications Administrator (for TAC/PS cmdlets)
      - User Administrator OR Global Administrator in Entra ID (for New-CsOnlineApplicationInstance)

    After running this script you MUST still complete Phase 3:
      Customer Service Admin Center → Channels → Phone Numbers → Manage
      → Advanced → Teams Phone System → sync each number into D365 Contact Center.
#>

[CmdletBinding(DefaultParameterSetName = 'Migrate', SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Migrate', 'Validate', 'Rollback')]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$CsvPath,

    [Parameter(Mandatory = $true)]
    [Guid]$DynamicsAppId,

    [Parameter(Mandatory = $true)]
    [Guid]$AcsResourceId,

    [ValidateSet('DirectRouting', 'OperatorConnect')]
    [string]$PhoneNumberType = 'DirectRouting',

    [string]$TxnLogPath = '.\tpe_migration_txnlog.jsonl',
    [string]$OutputPath  = '.\tpe_migration_results.csv',

    [switch]$DryRun,
    [switch]$AutoRollbackOnFailure,

    [int]$ThrottleMs            = 200,
    [int]$MaxRetries             = 3,
    [int]$InitialBackoffSeconds  = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

function Write-Txn {
    param([hashtable]$Record)
    $Record['Timestamp'] = (Get-Date -Format 'o')
    ($Record | ConvertTo-Json -Compress -Depth 10) | Add-Content -Path $TxnLogPath -Encoding UTF8
}

function Invoke-WithRetry {
    param(
        [scriptblock]$ScriptBlock,
        [string]$ActionName
    )
    $attempt = 0
    $delay   = [math]::Max(1, $InitialBackoffSeconds)
    while ($true) {
        try {
            $attempt++
            return (& $ScriptBlock)
        }
        catch {
            if ($attempt -ge $MaxRetries) { throw }
            Write-Warning "  ⚠  $ActionName failed (attempt $attempt/$MaxRetries): $($_.Exception.Message). Retrying in ${delay}s..."
            Start-Sleep -Seconds $delay
            $delay = [math]::Min(60, $delay * 2)
        }
    }
}

function Test-E164 {
    param([string]$PhoneNumber)
    return ($PhoneNumber -match '^\+[1-9]\d{7,14}$')
}

function Try-GetAppInstance {
    param([string]$Identity)
    try { return Get-CsOnlineApplicationInstance -Identity $Identity -ErrorAction Stop }
    catch { return $null }
}

function Get-PreviouslySucceeded {
    # Returns a HashSet of ResourceUpn values that already have Status=Succeeded in the txn log
    $succeeded = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    if (-not (Test-Path $TxnLogPath)) { return $succeeded }
    Get-Content -Path $TxnLogPath -Encoding UTF8 | ForEach-Object {
        try {
            $e = $_ | ConvertFrom-Json
            if ($e.Mode -eq 'Migrate' -and $e.Status -eq 'Succeeded') {
                [void]$succeeded.Add($e.ResourceUpn)
            }
        } catch { }
    }
    return $succeeded
}

function Write-SectionHeader {
    param([string]$Text)
    $line = '─' * 60
    Write-Host ""
    Write-Host $line -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host $line -ForegroundColor Cyan
}

function Write-StatusLine {
    param([int]$Index, [int]$Total, [string]$Upn, [string]$Phone, [string]$Status, [string]$Detail = '')
    $color = switch ($Status) {
        'OK'      { 'Green'  }
        'SKIPPED' { 'Yellow' }
        'FAILED'  { 'Red'    }
        'DRYRUN'  { 'Cyan'   }
        default   { 'White'  }
    }
    $pct     = [math]::Round(($Index / $Total) * 100)
    $padUpn  = $Upn.PadRight(50).Substring(0, [math]::Min(50, $Upn.Length)).PadRight(50)
    $padPhone= $Phone.PadRight(16)
    Write-Host ("[{0,4}/{1}]  {2}  {3}  {4}  {5}" -f $Index, $Total, $padPhone, $padUpn, $Status.PadRight(7), $Detail) -ForegroundColor $color
    Write-Progress -Activity "TPE Migration ($Mode)" -Status "$Index of $Total ($pct%)" -PercentComplete $pct
}

# ─────────────────────────────────────────────────────────────────────────────
#  Pre-flight
# ─────────────────────────────────────────────────────────────────────────────

Write-SectionHeader "TPE Bulk Number Migration  |  Mode: $Mode$(if ($DryRun) {' [DRY-RUN]'})"

# 1. Module check
if (-not (Get-Module -ListAvailable -Name MicrosoftTeams)) {
    throw "MicrosoftTeams module not found. Run: Install-Module MicrosoftTeams -Scope CurrentUser"
}
Import-Module MicrosoftTeams -ErrorAction Stop

# 2. Connect to Teams
Write-Host "Connecting to Microsoft Teams..." -ForegroundColor Yellow
Connect-MicrosoftTeams | Out-Null
Write-Host "Connected." -ForegroundColor Green

# 3. Load and validate CSV
if (-not (Test-Path $CsvPath)) { throw "CSV not found: $CsvPath" }
$rows = Import-Csv -Path $CsvPath
if (-not $rows -or $rows.Count -eq 0) { throw "CSV is empty: $CsvPath" }

# 4. Validate required columns
$requiredCols = @('ResourceUpn', 'DisplayName', 'PhoneNumber')
$csvHeaders   = ($rows[0].PSObject.Properties.Name)
foreach ($col in $requiredCols) {
    if ($col -notin $csvHeaders) {
        throw "CSV is missing required column: '$col'. Required columns: $($requiredCols -join ', ')"
    }
}

# 5. E.164 check + duplicate detection
$badNumbers    = @()
$seen          = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$seenUpns      = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($r in $rows) {
    $pn  = ($r.PhoneNumber ?? '').Trim()
    $upn = ($r.ResourceUpn  ?? '').Trim()
    if (-not (Test-E164 $pn))       { $badNumbers += $pn }
    if ($seen.Contains($pn))        { throw "Duplicate PhoneNumber in CSV: $pn" }
    if ($seenUpns.Contains($upn))   { throw "Duplicate ResourceUpn in CSV: $upn" }
    [void]$seen.Add($pn)
    [void]$seenUpns.Add($upn)
}
if ($badNumbers.Count -gt 0) {
    throw "CSV contains non-E.164 phone numbers (must be +<countrycode><number>, e.g. +12065551234).`nOffending values: $($badNumbers -join ', ')"
}

Write-Host "CSV loaded: $($rows.Count) rows. All phone numbers pass E.164 validation." -ForegroundColor Green

# ─────────────────────────────────────────────────────────────────────────────
#  MIGRATE MODE
# ─────────────────────────────────────────────────────────────────────────────

if ($Mode -eq 'Migrate') {

    $alreadySucceeded = Get-PreviouslySucceeded
    if ($alreadySucceeded.Count -gt 0) {
        Write-Host "Resuming: $($alreadySucceeded.Count) rows already succeeded in a previous run — will be skipped." -ForegroundColor Yellow
    }

    $results  = [System.Collections.Generic.List[pscustomobject]]::new()
    $counters = @{ Succeeded = 0; Failed = 0; Skipped = 0 }
    $startTime = Get-Date
    $total    = $rows.Count
    $index    = 0

    foreach ($r in $rows) {
        $index++
        $upn   = ($r.ResourceUpn  ?? '').Trim()
        $name  = ($r.DisplayName  ?? '').Trim()
        $phone = ($r.PhoneNumber  ?? '').Trim()
        if ([string]::IsNullOrWhiteSpace($name)) { $name = $upn }

        # Skip if already succeeded in a previous run
        if ($alreadySucceeded.Contains($upn)) {
            Write-StatusLine -Index $index -Total $total -Upn $upn -Phone $phone -Status 'SKIPPED' -Detail 'Already succeeded (txn log)'
            $counters.Skipped++
            $results.Add([pscustomobject]@{
                ResourceUpn = $upn; DisplayName = $name; PhoneNumber = $phone
                ObjectId = ''; Status = 'SKIPPED'; Step = 'N/A'; Error = 'Previously succeeded'
            })
            continue
        }

        $txnId    = [guid]::NewGuid().ToString()
        $objectId = $null
        $didAssign  = $false
        $createdNow = $false
        $rowStatus  = 'Started'
        $lastStep   = 'Init'
        $errMsg     = ''

        $txnRecord = @{
            TxnId = $txnId; Mode = 'Migrate'
            ResourceUpn = $upn; DisplayName = $name; PhoneNumber = $phone
            PhoneNumberType = $PhoneNumberType; DynamicsAppId = $DynamicsAppId.ToString()
            AcsResourceId = $AcsResourceId.ToString(); Status = $rowStatus
        }
        Write-Txn $txnRecord

        try {
            # ── Step A: Ensure resource account exists ──────────────────────
            $lastStep = 'EnsureAppInstance'
            $existing = Try-GetAppInstance -Identity $upn
            if (-not $existing) {
                if (-not $DryRun) {
                    Invoke-WithRetry -ActionName 'New-CsOnlineApplicationInstance' -ScriptBlock {
                        New-CsOnlineApplicationInstance `
                            -UserPrincipalName $upn `
                            -DisplayName $name `
                            -ApplicationId $DynamicsAppId `
                            -Force `
                            -ErrorAction Stop | Out-Null
                    }
                    Start-Sleep -Seconds 3   # Allow Entra ID replication
                    $existing = Invoke-WithRetry -ActionName 'Get-CsOnlineApplicationInstance (post-create)' -ScriptBlock {
                        Get-CsOnlineApplicationInstance -Identity $upn -ErrorAction Stop
                    }
                }
                $createdNow = $true
            }
            if (-not $DryRun) {
                $objectId = $existing.ObjectId
                $txnRecord['ObjectId'] = $objectId.ToString()
                $txnRecord['PreExisting'] = -not $createdNow
            }

            # ── Step B: Stamp ApplicationId + AcsResourceId ─────────────────
            $lastStep = 'StampAppIdAndAcsResourceId'
            if (-not $DryRun) {
                Invoke-WithRetry -ActionName 'Set-CsOnlineApplicationInstance' -ScriptBlock {
                    Set-CsOnlineApplicationInstance `
                        -Identity $objectId `
                        -ApplicationId $DynamicsAppId `
                        -AcsResourceId $AcsResourceId `
                        -Force `
                        -ErrorAction Stop | Out-Null
                }
            }

            # ── Step C: Sync into Agent Provisioning Service ─────────────────
            $lastStep = 'SyncToAPS'
            if (-not $DryRun) {
                Invoke-WithRetry -ActionName 'Sync-CsOnlineApplicationInstance' -ScriptBlock {
                    Sync-CsOnlineApplicationInstance `
                        -ObjectId $objectId `
                        -ApplicationId $DynamicsAppId `
                        -AcsResourceId $AcsResourceId `
                        -Force `
                        -ErrorAction Stop | Out-Null
                }
            }

            # ── Step D: Assign the Direct Routing phone number ───────────────
            $lastStep = 'AssignPhoneNumber'
            if (-not $DryRun) {
                Invoke-WithRetry -ActionName 'Set-CsPhoneNumberAssignment' -ScriptBlock {
                    Set-CsPhoneNumberAssignment `
                        -Identity $upn `
                        -PhoneNumber $phone `
                        -PhoneNumberType $PhoneNumberType `
                        -ErrorAction Stop | Out-Null
                }
                $didAssign = $true
            }

            # ── Success ──────────────────────────────────────────────────────
            $rowStatus = if ($DryRun) { 'DRYRUN' } else { 'Succeeded' }
            $txnRecord['Status']     = $rowStatus
            $txnRecord['CreatedNow'] = $createdNow
            $txnRecord['AssignedNow']= $didAssign
            Write-Txn $txnRecord

            Write-StatusLine -Index $index -Total $total -Upn $upn -Phone $phone `
                -Status (if ($DryRun) {'DRYRUN'} else {'OK'}) `
                -Detail (if ($createdNow) {'(new RA)'} else {'(existing RA)'})
            $counters.Succeeded++

        }
        catch {
            $errMsg    = $_.Exception.Message
            $rowStatus = 'FAILED'
            $txnRecord['Status']      = $rowStatus
            $txnRecord['Error']       = $errMsg
            $txnRecord['CreatedNow']  = $createdNow
            $txnRecord['AssignedNow'] = $didAssign
            $txnRecord['FailedStep']  = $lastStep
            Write-Txn $txnRecord

            Write-StatusLine -Index $index -Total $total -Upn $upn -Phone $phone -Status 'FAILED' -Detail $lastStep

            # Auto-rollback: only remove the number assignment if we assigned it in this run
            if ($AutoRollbackOnFailure -and $didAssign -and -not $DryRun) {
                Write-Warning "  AutoRollback: removing $phone from $upn..."
                try {
                    Invoke-WithRetry -ActionName 'Remove-CsPhoneNumberAssignment (auto-rollback)' -ScriptBlock {
                        Remove-CsPhoneNumberAssignment -Identity $upn -PhoneNumber $phone -PhoneNumberType $PhoneNumberType -ErrorAction Stop | Out-Null
                    }
                    $txnRecord['Rollback'] = 'AutoRollbackSucceeded'
                    Write-Txn $txnRecord
                }
                catch {
                    $txnRecord['Rollback']      = 'AutoRollbackFailed'
                    $txnRecord['RollbackError'] = $_.Exception.Message
                    Write-Txn $txnRecord
                    Write-Warning "  AutoRollback FAILED for $upn / $phone: $($_.Exception.Message)"
                }
            }

            $counters.Failed++
        }

        $results.Add([pscustomobject]@{
            ResourceUpn = $upn; DisplayName = $name; PhoneNumber = $phone
            ObjectId    = ($objectId ?? '')
            Status      = $rowStatus
            Step        = $lastStep
            Error       = $errMsg
        })

        if ($ThrottleMs -gt 0) { Start-Sleep -Milliseconds $ThrottleMs }
    }

    Write-Progress -Activity "TPE Migration" -Completed

    # Write results CSV
    $results | Export-Csv -Path $OutputPath -NoTypeInformation -Encoding UTF8

    # Summary
    $elapsed = (Get-Date) - $startTime
    Write-SectionHeader "MIGRATION SUMMARY$(if ($DryRun) {' [DRY-RUN]'})"
    Write-Host ("  Succeeded : {0}" -f $counters.Succeeded) -ForegroundColor Green
    Write-Host ("  Failed    : {0}" -f $counters.Failed)    -ForegroundColor $(if ($counters.Failed -gt 0) {'Red'} else {'White'})
    Write-Host ("  Skipped   : {0}" -f $counters.Skipped)   -ForegroundColor Yellow
    Write-Host ("  Total     : {0}" -f $total)
    Write-Host ("  Duration  : {0:hh\:mm\:ss}" -f $elapsed)
    Write-Host ("  Txn log   : $TxnLogPath")
    Write-Host ("  Results   : $OutputPath")

    if (-not $DryRun -and $counters.Succeeded -gt 0) {
        Write-Host ""
        Write-Host "  ✅ NEXT STEP (required):" -ForegroundColor Yellow
        Write-Host "     Open Customer Service Admin Center → Channels → Phone Numbers → Manage"
        Write-Host "     → Advanced → Teams Phone System"
        Write-Host "     Select all newly migrated numbers and sync them into D365 Contact Center."
    }
}

# ─────────────────────────────────────────────────────────────────────────────
#  VALIDATE MODE
# ─────────────────────────────────────────────────────────────────────────────

elseif ($Mode -eq 'Validate') {

    $results  = [System.Collections.Generic.List[pscustomobject]]::new()
    $counters = @{ Pass = 0; Fail = 0; Warn = 0 }
    $total    = $rows.Count
    $index    = 0

    Write-Host "Validating $total numbers against Teams state (read-only)..." -ForegroundColor Yellow

    foreach ($r in $rows) {
        $index++
        $upn   = ($r.ResourceUpn  ?? '').Trim()
        $phone = ($r.PhoneNumber  ?? '').Trim()

        Write-Progress -Activity 'TPE Validate' -Status "$index of $total" -PercentComplete ([math]::Round(($index/$total)*100))

        $checkResult = 'UNKNOWN'
        $detail      = ''

        try {
            $instance = Try-GetAppInstance -Identity $upn
            if (-not $instance) {
                $checkResult = 'FAIL'
                $detail = 'Resource account NOT FOUND in Teams'
                $counters.Fail++
            }
            else {
                # Check number assignment
                $assigned = Get-CsPhoneNumberAssignment -AssignedPstnTargetId $instance.ObjectId -ErrorAction SilentlyContinue
                $matchedPhone = $assigned | Where-Object { $_.TelephoneNumber -eq $phone }

                if ($matchedPhone) {
                    # Check ApplicationId is stamped
                    $appIdMatch = $instance.ApplicationId -eq $DynamicsAppId.ToString()
                    if ($appIdMatch) {
                        $checkResult = 'PASS'
                        $detail = "ObjectId: $($instance.ObjectId)"
                        $counters.Pass++
                    } else {
                        $checkResult = 'WARN'
                        $detail = "Number assigned but ApplicationId mismatch (expected: $DynamicsAppId, actual: $($instance.ApplicationId))"
                        $counters.Warn++
                    }
                }
                else {
                    $checkResult = 'FAIL'
                    $detail = "Resource account exists but number $phone is NOT assigned to it"
                    $counters.Fail++
                }
            }
        }
        catch {
            $checkResult = 'FAIL'
            $detail = $_.Exception.Message
            $counters.Fail++
        }

        $color = switch ($checkResult) { 'PASS' {'Green'} 'WARN' {'Yellow'} default {'Red'} }
        Write-Host ("[{0,4}/{1}]  {2,-16}  {3,-50}  {4}" -f $index, $total, $phone, $upn, $checkResult) -ForegroundColor $color
        if ($detail -and $checkResult -ne 'PASS') { Write-Host ("             ↳ $detail") -ForegroundColor $color }

        $results.Add([pscustomobject]@{
            ResourceUpn = $upn; PhoneNumber = $phone
            Result = $checkResult; Detail = $detail
        })
    }

    Write-Progress -Activity 'TPE Validate' -Completed
    $results | Export-Csv -Path ($OutputPath -replace '\.csv$', '_validation.csv') -NoTypeInformation -Encoding UTF8

    Write-SectionHeader 'VALIDATION SUMMARY'
    Write-Host ("  PASS : {0}" -f $counters.Pass) -ForegroundColor Green
    Write-Host ("  WARN : {0}" -f $counters.Warn) -ForegroundColor Yellow
    Write-Host ("  FAIL : {0}" -f $counters.Fail) -ForegroundColor $(if ($counters.Fail -gt 0) {'Red'} else {'White'})
    Write-Host ("  Total: {0}" -f $total)
    Write-Host ("  Report: $($OutputPath -replace '\.csv$', '_validation.csv')")
}

# ─────────────────────────────────────────────────────────────────────────────
#  ROLLBACK MODE
# ─────────────────────────────────────────────────────────────────────────────

elseif ($Mode -eq 'Rollback') {

    if (-not (Test-Path $TxnLogPath)) {
        throw "Transaction log not found: $TxnLogPath. Cannot rollback without the log produced by -Mode Migrate."
    }

    # Parse txn log — keep final state per TxnId
    $lines      = Get-Content -Path $TxnLogPath -Encoding UTF8
    $finalByTxn = @{}
    foreach ($line in $lines) {
        try {
            $e = $line | ConvertFrom-Json
            if ($e.TxnId) { $finalByTxn[$e.TxnId] = $e }
        } catch { }
    }

    # Filter: only Migrate-Succeeded rows where we actually assigned the number
    $toRollback = $finalByTxn.Values |
        Where-Object { $_.Mode -eq 'Migrate' -and $_.Status -eq 'Succeeded' -and $_.AssignedNow -eq $true }

    if ($toRollback.Count -eq 0) {
        Write-Host "Nothing to rollback — no successfully-assigned rows found in transaction log." -ForegroundColor Yellow
        exit 0
    }

    Write-Host "Found $($toRollback.Count) rows to roll back." -ForegroundColor Yellow
    if (-not $DryRun) {
        $confirm = Read-Host "Type 'ROLLBACK' to confirm removal of $($toRollback.Count) phone number assignments"
        if ($confirm -ne 'ROLLBACK') { Write-Host "Cancelled."; exit 0 }
    }

    $rbCount = 0; $rbFailed = 0
    foreach ($e in $toRollback) {
        $upn   = $e.ResourceUpn
        $phone = $e.PhoneNumber
        try {
            if (-not $DryRun) {
                Invoke-WithRetry -ActionName "Remove-CsPhoneNumberAssignment ($upn)" -ScriptBlock {
                    Remove-CsPhoneNumberAssignment `
                        -Identity $upn `
                        -PhoneNumber $phone `
                        -PhoneNumberType $PhoneNumberType `
                        -ErrorAction Stop | Out-Null
                }
            }
            Write-Host "  ROLLED BACK: $upn  ←  $phone" -ForegroundColor Yellow
            Write-Txn @{ TxnId=$e.TxnId; Mode='Rollback'; ResourceUpn=$upn; PhoneNumber=$phone; Status='RolledBack'; DryRun=[bool]$DryRun }
            $rbCount++
        }
        catch {
            Write-Warning "  ROLLBACK FAILED: $upn / $phone — $($_.Exception.Message)"
            Write-Txn @{ TxnId=$e.TxnId; Mode='Rollback'; ResourceUpn=$upn; PhoneNumber=$phone; Status='RollbackFailed'; Error=$_.Exception.Message }
            $rbFailed++
        }
        if ($ThrottleMs -gt 0) { Start-Sleep -Milliseconds $ThrottleMs }
    }

    Write-SectionHeader 'ROLLBACK SUMMARY'
    Write-Host ("  Rolled back : {0}" -f $rbCount)  -ForegroundColor Yellow
    Write-Host ("  Failed      : {0}" -f $rbFailed) -ForegroundColor $(if ($rbFailed -gt 0) {'Red'} else {'White'})
    Write-Host "  Txn log updated: $TxnLogPath"
    Write-Host ""
    Write-Host "  ⚠  Note: Rollback only removes Teams phone number assignments." -ForegroundColor Yellow
    Write-Host "     Resource accounts (application instances) created by this script are NOT deleted." -ForegroundColor Yellow
    Write-Host "     To fully undo, also re-add your SBC configuration in the Azure Portal → ACS resource." -ForegroundColor Yellow
}
