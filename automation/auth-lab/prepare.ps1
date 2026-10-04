$ErrorActionPreference = 'Stop'
$labRoot = $PSScriptRoot
$secretDir = Join-Path $labRoot 'secrets'
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null

$envFile = Join-Path $labRoot '.env.local'
$envNames = @('LAB_DB_PASSWORD', 'LAB_LDAP_ADMIN_PASSWORD', 'LAB_LDAP_READ_PASSWORD', 'LAB_MOODLE_ADMIN_PASSWORD', 'LAB_RECOVERY_TOKEN')
$template = if (Test-Path -LiteralPath $envFile) {
    Get-Content -Raw -LiteralPath $envFile
} else {
    Get-Content -Raw (Join-Path $labRoot '.env.example')
}
foreach ($name in $envNames) {
    $match = [regex]::Match($template, "(?m)^$name=(.*)$")
    if (-not $match.Success -or [string]::IsNullOrWhiteSpace($match.Groups[1].Value) -or $match.Groups[1].Value -eq 'replace-this-local-only') {
        $bytes = [byte[]]::new(24)
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $value = ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
        $rng.Dispose()
        if ($match.Success) {
            $template = [regex]::Replace($template, "(?m)^$name=.*$", "$name=$value")
        } else {
            $template = $template.TrimEnd() + "`n$name=$value`n"
        }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}
Set-Content -NoNewline -Encoding ascii -LiteralPath $envFile -Value $template

$settings = @{}
Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^([A-Z0-9_]+)=(.*)$') { $settings[$matches[1]] = $matches[2] }
}
foreach ($required in @('LAB_DB_PASSWORD', 'LAB_LDAP_ADMIN_PASSWORD', 'LAB_LDAP_READ_PASSWORD', 'LAB_MOODLE_ADMIN_PASSWORD', 'LAB_RECOVERY_TOKEN')) {
    if ([string]::IsNullOrWhiteSpace($settings[$required]) -or $settings[$required] -eq 'replace-this-local-only') {
        throw "Missing generated local-only value for $required in automation/auth-lab/.env.local"
    }
}

$files = @{
    'moodle-db-password' = $settings['LAB_DB_PASSWORD']
    'moodle-admin-password' = $settings['LAB_MOODLE_ADMIN_PASSWORD']
    'ldap-reader-password' = $settings['LAB_LDAP_READ_PASSWORD']
    'auth-user' = 'authlab-user'
    'auth-password' = 'lab-only-password'
}
foreach ($name in $files.Keys) {
    Set-Content -NoNewline -Encoding ascii -LiteralPath (Join-Path $secretDir $name) -Value $files[$name]
}

$composeFile = Join-Path $labRoot 'docker-compose.yml'
docker compose --env-file $envFile -f $composeFile build openldap moodle-web ai-agent celery-worker
if ($LASTEXITCODE -ne 0) { throw 'Failed to build the local Moodle/OpenLDAP/AI-agent images.' }
$certDir = Join-Path $secretDir 'certs'
New-Item -ItemType Directory -Force -Path $certDir | Out-Null
$certDir = (Resolve-Path -LiteralPath $certDir).Path
$generateScript = Join-Path $labRoot 'scripts/generate-certs.sh'
docker run --rm --user 0:0 --mount "type=bind,source=$certDir,target=/certs" --mount "type=bind,source=$generateScript,target=/generate-certs.sh,readonly" --entrypoint sh local/moodle:auth-lab /generate-certs.sh
if ($LASTEXITCODE -ne 0) { throw 'Failed to generate local LDAPS certificates.' }
docker compose --env-file $envFile -f $composeFile run --rm cert-init
if ($LASTEXITCODE -ne 0) { throw 'Failed to seed the generated LDAPS certificates into the local Docker volume.' }
Write-Output 'Local auth-lab configuration and ignored secret files are ready. Password values were not printed.'
