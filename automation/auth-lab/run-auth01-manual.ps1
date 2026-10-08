$ErrorActionPreference = 'Stop'
$labRoot = $PSScriptRoot
$envFile = Join-Path $labRoot '.env.local'
$composeFile = Join-Path $labRoot 'docker-compose.yml'
if (-not (Test-Path $envFile)) { throw 'Local lab is missing. Run start-floci-moodle.ps1 first.' }

function Get-Metric([string]$Query) {
    $encoded = [uri]::EscapeDataString($Query)
    $response = Invoke-RestMethod "http://127.0.0.1:19090/api/v1/query?query=$encoded" -TimeoutSec 8
    if ($response.status -ne 'success' -or $response.data.result.Count -ne 1) { return $null }
    return [double]$response.data.result[0].value[1]
}

function Set-EnvValue([string]$Name, [string]$Value) {
    $content = Get-Content -Raw $envFile
    if ($content -match "(?m)^$Name=") { $content = [regex]::Replace($content, "(?m)^$Name=.*$", "$Name=$Value") }
    else { $content = $content.TrimEnd() + "`n$Name=$Value`n" }
    Set-Content -NoNewline -Encoding ascii -LiteralPath $envFile -Value $content
}

# Select a genuinely empty Redis DB so an earlier resolved incident cannot be reused.
$db = $null
foreach ($candidate in 1..15) {
    $size = docker exec moodle-auth-lab-redis-1 redis-cli -n $candidate dbsize 2>$null
    if ($LASTEXITCODE -eq 0 -and "$size".Trim() -eq '0') { $db = $candidate; break }
}
if ($null -eq $db) { throw 'No empty Redis DB is available (checked DB 1 through 15); no fault was injected.' }

foreach ($check in @(
    @{Name='Moodle health'; Query='probe_success{job="moodle"}'}
    @{Name='ALB health'; Query='probe_success{job="moodle_alb"}'}
    @{Name='LDAPS'; Query='probe_success{job="openldap_ldaps"}'}
    @{Name='Valid Moodle login'; Query='auth_lab_valid_login_success'}
)) {
    $value = Get-Metric $check.Query
    if ($value -ne 1) { throw "$($check.Name) preflight is not healthy; no fault was injected." }
}
$login = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18082/login/index.php' -TimeoutSec 10
if ($login.StatusCode -ne 200) { throw 'Moodle ALB login page is not HTTP 200; no fault was injected.' }

Set-EnvValue 'LAB_REDIS_DB' "$db"
docker compose --env-file $envFile -f $composeFile up -d --force-recreate ai-agent celery-worker
if ($LASTEXITCODE -ne 0) { throw 'Could not isolate this test in a clean Redis DB; no fault was injected.' }
$deadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 5
    $valid = Get-Metric 'auth_lab_valid_login_success'
} while ($valid -ne 1 -and (Get-Date) -lt $deadline)
if ($valid -ne 1) { throw 'Valid login did not recover after the clean worker start; no fault was injected.' }

$started = [DateTime]::UtcNow
$identity = 'AUTH-01:authlab-openldap'
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try { $sha = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity)) }
finally { $sha256.Dispose() }
$incidentId = 'auth-' + ([BitConverter]::ToString($sha).Replace('-', '').ToLowerInvariant().Substring(0,16))
Write-Output "Injecting AUTH-01 by stopping only Compose service openldap. Isolated Redis DB: $db."
docker compose --env-file $envFile -f $composeFile stop openldap
if ($LASTEXITCODE -ne 0) { throw 'Could not stop OpenLDAP; no incident was created.' }

$resolved = $false
$incidentContext = $null
$deadline = (Get-Date).AddMinutes(10)
try {
    do {
        Start-Sleep -Seconds 5
        $raw = docker exec moodle-auth-lab-redis-1 redis-cli -n $db --raw get "incident:$incidentId" 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            try { $incidentContext = ($raw -join "`n") | ConvertFrom-Json } catch { $incidentContext = $null }
            if ($incidentContext.status -eq 'RESOLVED' -and $incidentContext.resolution_authority -eq 'independent_verifier') { $resolved = $true; break }
            if ($incidentContext.status -in @('ESCALATED','FAILED')) { break }
        }
    } while ((Get-Date) -lt $deadline)
} finally {
    $state = docker inspect --format '{{.State.Running}}' moodle-auth-lab-openldap-1 2>$null
    if ($LASTEXITCODE -eq 0 -and "$state".Trim() -ne 'true') {
        docker compose --env-file $envFile -f $composeFile start openldap | Out-Null
    }
}

$proof = [ordered]@{
    scenario = 'AUTH-01'
    started_utc = $started.ToString('o')
    finished_utc = [DateTime]::UtcNow.ToString('o')
    incident_id = $incidentId
    isolated_redis_db = $db
    status = if ($incidentContext) { $incidentContext.status } else { 'NO_INCIDENT_CONTEXT' }
    resolution_authority = if ($incidentContext) { $incidentContext.resolution_authority } else { $null }
    verifier = if ($incidentContext) { $incidentContext.verifier } else { $null }
    alertmanager_auth01_active_alerts = @(
        (Invoke-RestMethod 'http://127.0.0.1:19093/api/v2/alerts' -TimeoutSec 8) |
            Where-Object { $_.labels.scenario -eq 'AUTH-01' -and $_.status.state -eq 'active' }
    ).Count
    probes_after_run = @{
        moodle_health = Get-Metric 'probe_success{job="moodle"}'
        alb_health = Get-Metric 'probe_success{job="moodle_alb"}'
        ldaps = Get-Metric 'probe_success{job="openldap_ldaps"}'
        valid_login = Get-Metric 'auth_lab_valid_login_success'
    }
}
$artifact = Join-Path $labRoot ('secrets/AUTH-01-' + $started.ToString('yyyyMMdd-HHmmss') + '.json')
$proof | ConvertTo-Json -Depth 12 | Set-Content -Encoding utf8 -LiteralPath $artifact
Write-Output "Evidence JSON: $artifact"
Write-Output ($proof | ConvertTo-Json -Depth 8)
if (-not $resolved) { throw 'AUTH-01 did not reach verifier-owned RESOLVED. The script restarted OpenLDAP for safety; this is a failed trial, not recovery proof.' }
if (@($proof.probes_after_run.Values | Where-Object { $_ -ne 1 }).Count -gt 0) { throw 'The incident resolved, but one or more post-run probes are not healthy.' }
if ($proof.alertmanager_auth01_active_alerts -ne 0) { throw 'The incident resolved, but an AUTH-01 alert is still active.' }
Write-Output 'PASS: AUTH-01 resolved by the independent verifier after valid/invalid login, Moodle, LDAPS, and 120-second stability checks.'
