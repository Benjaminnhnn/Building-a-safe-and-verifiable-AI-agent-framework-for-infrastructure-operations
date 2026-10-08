[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('floci', 'aws')]
    [string]$Target,

    [ValidateSet('show', 'start', 'plan')]
    [string]$Action = 'show'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

switch ($Target.ToLowerInvariant()) {
    'floci' {
        $terraformRoot = Join-Path $repoRoot 'terraform/local-floci'
        if ($Action -eq 'plan') {
            terraform "-chdir=$terraformRoot" plan -input=false
            if ($LASTEXITCODE -ne 0) { throw 'Floci Terraform plan failed.' }
            break
        }

        if ($Action -eq 'start') {
            & (Join-Path $PSScriptRoot 'auth-lab/start-floci-moodle.ps1')
            if ($LASTEXITCODE -ne 0) { throw 'Floci local stack failed to start.' }
            break
        }

        Write-Output 'Target: Floci (local only; loopback endpoint 127.0.0.1:4566)'
        Write-Output "Terraform root: $terraformRoot"
        Write-Output 'Runtime: Docker Compose Moodle/AIOps lab'
        Write-Output 'Start: .\automation\select-cloud-profile.ps1 -Target floci -Action start'
    }
    'aws' {
        $terraformRoot = Join-Path $repoRoot 'terraform'
        if (-not [string]::IsNullOrWhiteSpace($env:AWS_ENDPOINT_URL)) {
            throw 'AWS_ENDPOINT_URL is set. Clear the emulator override before selecting the AWS target.'
        }
        if ([string]::IsNullOrWhiteSpace($env:AWS_PROFILE)) {
            throw 'Set AWS_PROFILE to the intended named AWS profile before selecting the AWS target.'
        }
        if ($Action -eq 'start') {
            throw 'Direct AWS start/apply is intentionally not provided here. Review the plan and use the reviewed staging deployment workflow.'
        }
        if ($Action -eq 'plan') {
            terraform "-chdir=$terraformRoot" plan -input=false
            if ($LASTEXITCODE -ne 0) { throw 'AWS Terraform plan failed.' }
            break
        }

        Write-Output 'Target: AWS (named profile required; this selector does not apply resources)'
        Write-Output "Terraform root: $terraformRoot"
        Write-Output "AWS profile selected: $env:AWS_PROFILE"
        Write-Output 'Plan: .\automation\select-cloud-profile.ps1 -Target aws -Action plan'
        Write-Output 'No AWS resources have been changed.'
    }
}
