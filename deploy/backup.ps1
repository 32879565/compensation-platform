[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"

function Get-PostgresEnvironmentValue {
    param(
        [Parameter(Mandatory)]
        [string]$ContainerId,
        [Parameter(Mandatory)]
        [string]$Name
    )

    $value = (& docker exec $ContainerId printenv $Name | Out-String).Trim()
    if (-not $value) {
        throw "PostgreSQL container does not define a usable $Name value."
    }
    return $value
}

$deployDir = Split-Path -Parent $PSCommandPath
$composeFile = Join-Path $deployDir "docker-compose.yml"
$outputDirectory = Split-Path -Parent $OutputPath
$outputLeaf = Split-Path -Leaf $OutputPath

if (-not $outputDirectory -or -not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    throw "The backup output directory must already exist: $outputDirectory"
}

$resolvedOutput = Join-Path (Resolve-Path -LiteralPath $outputDirectory) $outputLeaf
if (Test-Path -LiteralPath $resolvedOutput) {
    throw "Refusing to overwrite an existing backup: $resolvedOutput"
}
$checksumPath = "$resolvedOutput.sha256"
if (Test-Path -LiteralPath $checksumPath) {
    throw "Refusing to overwrite an existing backup checksum: $checksumPath"
}

$postgresId = (& docker compose -f $composeFile ps -q postgres | Out-String).Trim()
if (-not $postgresId) {
    throw "The postgres compose service is not running. Start it before creating a backup."
}
$postgresUser = Get-PostgresEnvironmentValue -ContainerId $postgresId -Name "POSTGRES_USER"
$postgresDatabase = Get-PostgresEnvironmentValue -ContainerId $postgresId -Name "POSTGRES_DB"

$containerArchive = "/tmp/compensation-backup-$([guid]::NewGuid().ToString('N')).dump"
try {
    # Pass every Docker argument separately. This avoids Windows PowerShell's
    # legacy native-argument reserialization of a shell command string.
    & docker exec $postgresId pg_dump -U $postgresUser -d $postgresDatabase -Fc -f $containerArchive
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed."
    }
    & docker cp "${postgresId}:$containerArchive" $resolvedOutput
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to copy the PostgreSQL archive from the container."
    }
}
finally {
    # This path is generated in this script, so cleanup cannot target a
    # user-supplied container path.
    & docker exec $postgresId rm -f -- $containerArchive 2>$null
}

$digest = (Get-FileHash -LiteralPath $resolvedOutput -Algorithm SHA256).Hash
$digest | Set-Content -LiteralPath $checksumPath -Encoding ascii -NoNewline
Write-Output "Backup created: $resolvedOutput"
Write-Output "SHA256: $digest"
