[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$BackupPath,
    [Parameter(Mandatory)]
    [string]$EmergencyBackupPath,
    [switch]$Force
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

if (-not $Force) {
    throw "Restore replaces the current payroll database. Re-run with -Force after reading docs/operations.md."
}
if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
    throw "Backup archive was not found: $BackupPath"
}

$resolvedBackup = (Resolve-Path -LiteralPath $BackupPath).Path
$resolvedEmergencyPath = [System.IO.Path]::GetFullPath($EmergencyBackupPath)
if ([string]::Equals($resolvedBackup, $resolvedEmergencyPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "EmergencyBackupPath must be a new archive path, not the archive being restored."
}
$checksumPath = "$resolvedBackup.sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "Backup checksum is required beside the archive: $checksumPath"
}
$expectedDigest = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0]
if ($expectedDigest -notmatch '^[A-Fa-f0-9]{64}$') {
    throw "Backup checksum file does not contain a valid SHA256 digest: $checksumPath"
}
$actualDigest = (Get-FileHash -LiteralPath $resolvedBackup -Algorithm SHA256).Hash
if (-not [string]::Equals($expectedDigest, $actualDigest, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup SHA256 does not match $checksumPath; refusing to restore an unverified archive."
}
$deployDir = Split-Path -Parent $PSCommandPath
$composeFile = Join-Path $deployDir "docker-compose.yml"
$runningBackend = (& docker compose -f $composeFile ps --status running -q backend | Out-String).Trim()
if ($runningBackend) {
    throw "Stop the backend before restoring so no payroll write can race with the restore."
}

$postgresId = (& docker compose -f $composeFile ps -q postgres | Out-String).Trim()
if (-not $postgresId) {
    throw "The postgres compose service is not running. Start only postgres before restoring."
}
$postgresUser = Get-PostgresEnvironmentValue -ContainerId $postgresId -Name "POSTGRES_USER"
$postgresDatabase = Get-PostgresEnvironmentValue -ContainerId $postgresId -Name "POSTGRES_DB"

$containerArchive = "/tmp/compensation-restore-$([guid]::NewGuid().ToString('N')).dump"
try {
    & docker cp $resolvedBackup "${postgresId}:$containerArchive"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to copy the PostgreSQL archive into the container."
    }
    # Validate the custom archive before deleting current application data.
    & docker exec $postgresId pg_restore --list $containerArchive | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Backup archive failed pg_restore preflight; the current database is unchanged."
    }
    # Capture the current state before any destructive action. The backup helper
    # refuses overwrites and writes its own SHA256 sidecar, so an interrupted or
    # incompatible restore always has a verified rollback archive.
    & (Join-Path $deployDir "backup.ps1") -OutputPath $EmergencyBackupPath
    # pg_restore --clean removes only objects it knows about from the archive.
    # Recreate public first so a restore from an older backup cannot leave a
    # later migration's table/column behind and corrupt the next upgrade.
    # Feed SQL over stdin rather than embedding it in a native command-line
    # argument. Windows PowerShell 5.1 can otherwise split the SQL at spaces.
    $recreateSchemaSql = @'
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO CURRENT_USER;
'@
    $recreateSchemaSql | & docker exec -i $postgresId psql -v ON_ERROR_STOP=1 -U $postgresUser -d $postgresDatabase
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to recreate the application schema before restore."
    }
    & docker exec $postgresId pg_restore --exit-on-error --no-owner -U $postgresUser -d $postgresDatabase $containerArchive
    if ($LASTEXITCODE -ne 0) {
        throw "pg_restore failed; leave the backend stopped and investigate before retrying."
    }
}
finally {
    # This path is generated in this script, so cleanup cannot target a
    # user-supplied container path.
    & docker exec $postgresId rm -f -- $containerArchive 2>$null
}

Write-Output "Restore completed. Start the backend; its entrypoint will run alembic upgrade head before serving traffic."
