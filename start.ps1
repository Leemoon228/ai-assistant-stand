param([switch]$Monitoring)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function New-StandSecret {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($bytes)
}

if (-not (Test-Path -LiteralPath 'manifest.json')) {
    throw 'Download and extract a stand release before running start.ps1.'
}
$manifest = Get-Content -LiteralPath 'manifest.json' -Raw | ConvertFrom-Json
foreach ($component in $manifest.components.PSObject.Properties) {
    $directory = Join-Path 'components' $component.Name
    foreach ($entry in $component.Value.checksums.PSObject.Properties) {
        if ($entry.Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') {
            throw 'Invalid release asset filename.'
        }
        $asset = Join-Path $directory $entry.Name
        if ((Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash -ne $entry.Value) {
            throw "Checksum mismatch in $asset"
        }
    }
    foreach ($image in $component.Value.manifest.images) {
        $archive = Join-Path $directory $image.archive
        docker image load --input $archive
        if ($LASTEXITCODE -ne 0) { throw "Cannot load $archive" }
        $imageId = docker image inspect --format '{{.Id}}' $image.image
        if ($LASTEXITCODE -ne 0 -or $imageId -ne $image.image_id) {
            throw "Unexpected image identity for $($image.service)"
        }
    }
}

if (-not (Test-Path -LiteralPath '.env')) {
    $configuration = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '.env.example'))
    $configuration = $configuration.Replace('SSO_SHARED_SECRET=', ('SSO_SHARED_SECRET=' + (New-StandSecret)))
    $configuration = $configuration.Replace('GRAFANA_ADMIN_PASSWORD=', ('GRAFANA_ADMIN_PASSWORD=' + (New-StandSecret)))
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot '.env'), $configuration, [Text.UTF8Encoding]::new($false))
    Write-Output 'Created local .env with unique stand credentials. Keep this file private.'
}

$arguments = @('compose', '--env-file', '.env', '-f', 'compose.yml')
if ($Monitoring) { $arguments += @('--profile', 'monitoring') }
$arguments += @('up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
& docker @arguments
if ($LASTEXITCODE -ne 0) { throw 'Stand startup failed. Inspect docker compose logs.' }
Write-Output 'Marketplace http://localhost:8081. Assistant http://localhost:8080.'
