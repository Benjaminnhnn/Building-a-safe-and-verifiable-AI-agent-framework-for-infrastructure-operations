$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$envFile = Join-Path $PSScriptRoot '.env.local'
$composeFile = Join-Path $PSScriptRoot 'docker-compose.yml'
$terraformDir = Join-Path $repoRoot 'terraform/local-floci'
$prepared = $false

if (-not (Test-Path $envFile) -or -not (Test-Path (Join-Path $PSScriptRoot 'secrets/moodle-db-password'))) {
    & (Join-Path $PSScriptRoot 'prepare.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Auth lab preparation failed.' }
    $prepared = $true
}

$settings = @{}
Get-Content $envFile | ForEach-Object { if ($_ -match '^([A-Z0-9_]+)=(.*)$') { $settings[$matches[1]] = $matches[2] } }
if ([string]::IsNullOrWhiteSpace($settings['LAB_DB_PASSWORD'])) { throw 'LAB_DB_PASSWORD is missing from .env.local.' }

# Keep the local Moodle endpoint and Floci RDS proxy settings stable across runs.
$required = [ordered]@{
    LAB_MOODLE_WWWROOT = 'http://127.0.0.1:18082'
    LAB_MOODLE_DB_HOST = 'host.docker.internal'
    LAB_MOODLE_DB_PORT = '7001'
    LAB_MOODLE_DB_USER = 'moodle_local'
    LAB_MOODLE_DB_NAME = 'moodle'
    LAB_MOODLE_CANONICAL_HOST = '127.0.0.1:18082'
}
$lines = @(Get-Content $envFile | Where-Object { $_ -notmatch '^LAB_MOODLE_(WWWROOT|DB_HOST|DB_PORT|DB_USER|DB_NAME|CANONICAL_HOST)=' })
foreach ($key in $required.Keys) { $lines += "$key=$($required[$key])" }
Set-Content -Encoding ascii -LiteralPath $envFile -Value $lines

if (-not $prepared) {
    docker compose --env-file $envFile -f $composeFile build moodle-web
    if ($LASTEXITCODE -ne 0) { throw 'Moodle image build failed.' }
}

$saved = @{}
foreach ($name in @('AWS_ENDPOINT_URL','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_DEFAULT_REGION','AWS_REGION','TF_VAR_moodle_db_password','TF_VAR_moodle_target_ips')) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:AWS_ENDPOINT_URL = 'http://127.0.0.1:4566'
    $env:AWS_ACCESS_KEY_ID = 'local-only'
    $env:AWS_SECRET_ACCESS_KEY = 'local-only'
    $env:AWS_DEFAULT_REGION = 'us-east-1'
    $env:AWS_REGION = 'us-east-1'
    $env:TF_VAR_moodle_db_password = $settings['LAB_DB_PASSWORD']
    $existingIps = @()
    foreach ($service in @('moodle-web','moodle-web-b')) {
        $existingId = docker compose --env-file $envFile -f $composeFile ps -q $service | Select-Object -First 1
        if ($existingId) {
            $existingIp = docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $existingId
            if (-not [string]::IsNullOrWhiteSpace($existingIp)) { $existingIps += $existingIp }
        }
    }
    $env:TF_VAR_moodle_target_ips = '[' + (($existingIps | ForEach-Object { '"' + $_ + '"' }) -join ',') + ']'

    docker compose --env-file $envFile -f $composeFile up -d floci redis cert-init
    if ($LASTEXITCODE -ne 0) { throw 'Failed to start Floci and local prerequisites.' }
    $deadline = (Get-Date).AddMinutes(3)
    do {
        try { $health = Invoke-RestMethod 'http://127.0.0.1:4566/_floci/health'; if ($health) { break } } catch {}
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    if (-not $health) { throw 'Floci did not become healthy within 3 minutes.' }

    terraform "-chdir=$terraformDir" init -input=false
    if ($LASTEXITCODE -ne 0) { throw 'Terraform init failed.' }
    terraform "-chdir=$terraformDir" apply -auto-approve -input=false
    if ($LASTEXITCODE -ne 0) { throw 'Terraform could not provision local Floci resources.' }

    # Install only on a new, empty Floci RDS database; existing Moodle data is retained.
    $rds = docker ps --filter 'name=floci-aws-rds-db-' --format '{{.Names}}' | Select-Object -First 1
    if (-not $rds) { throw 'Floci RDS PostgreSQL container was not found.' }
    $installed = docker exec $rds psql -U moodle_local -d moodle -Atc "SELECT to_regclass('public.mdl_config') IS NOT NULL" 2>$null
    $installed = ($LASTEXITCODE -eq 0 -and "$installed".Trim() -eq 't')
    if (-not $installed) {
        docker compose --env-file $envFile -f $composeFile --profile setup run --rm moodle-install
        if ($LASTEXITCODE -ne 0) { throw 'Moodle CLI installation failed.' }
    }

    docker compose --env-file $envFile -f $composeFile up -d
    if ($LASTEXITCODE -ne 0) { throw 'Failed to start the complete Moodle and AI operations stack.' }
    $ips = @()
    foreach ($service in @('moodle-web','moodle-web-b')) {
        $id = docker compose --env-file $envFile -f $composeFile ps -q $service | Select-Object -First 1
        if (-not $id) { throw "Compose did not create $service." }
        $ip = docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $id
        if ([string]::IsNullOrWhiteSpace($ip)) { throw "Could not read the Docker IP for $service." }
        $ips += $ip
    }
    $env:TF_VAR_moodle_target_ips = '[' + (($ips | ForEach-Object { '"' + $_ + '"' }) -join ',') + ']'
    terraform "-chdir=$terraformDir" apply -auto-approve -input=false
    if ($LASTEXITCODE -ne 0) { throw 'Terraform could not register both Moodle web replicas in the Floci ALB.' }

    # The legacy local database is profile-gated and is not started by `up`.
    $deadline = (Get-Date).AddMinutes(4)
    do {
        try {
            $login = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18082/login/index.php' -TimeoutSec 8
            if ($login.StatusCode -eq 200) { break }
        } catch {}
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    if (-not $login -or $login.StatusCode -ne 200) { throw 'Moodle login page did not return HTTP 200 from the primary node.' }
    $albHealth = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18081/healthz.php' -TimeoutSec 8
    if ($albHealth.StatusCode -ne 200) { throw 'Moodle health through the Floci ALB is not HTTP 200.' }

    Write-Output 'Local Floci Moodle stack is up.'
    Write-Output 'Moodle URL: http://127.0.0.1:18082'
    Write-Output 'Floci ALB health endpoint: http://127.0.0.1:18081/healthz.php'
    Write-Output 'Monitoring: http://127.0.0.1:19090 | Alertmanager: http://127.0.0.1:19093'
    Write-Output 'Moodle login returned HTTP 200 directly; both web services and ALB health targets are managed by Compose/Terraform.'
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
