# 手动打包入口：仅收集源码白名单并生成校验文件；不由启动或日常源码更新自动调用。
param(
    [string]$Version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim(),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'dist')
)

$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
    throw "Invalid version: $Version"
}

$requiredFiles = @(
    '.gitignore',
    'VERSION',
    'README.md',
    'ARCHITECTURE.md',
    'VALIDATION.md',
    'config.json',
    'package.ps1',
    'review-prompt.md',
    'review-schema.json',
    'start.ps1',
    'tests/__init__.py',
    'tests/test_watcher.py',
    'tests/test_components.py',
    'tests/test_console.py',
    'tests/test_retention.py',
    'tests/test_visibility.py',
    'tests/test_status.py',
    'watcher.py'
)
$requiredFiles += @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'review_watcher') -Filter '*.py' -File | ForEach-Object { 'review_watcher/' + $_.Name })
foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $relativePath))) {
        throw "Missing package file: $relativePath"
    }
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("gitcode-review-watcher-package-" + [guid]::NewGuid())
$bundleName = "gitcode-pr-review-watcher-$Version"
$bundleDirectory = Join-Path $temporaryRoot $bundleName
$archive = Join-Path $OutputDirectory "$bundleName.zip"
$checksum = "$archive.sha256"

try {
    New-Item -ItemType Directory -Path $bundleDirectory -Force | Out-Null
    foreach ($relativePath in $requiredFiles) {
        $destination = Join-Path $bundleDirectory $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relativePath) -Destination $destination
    }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    if (Test-Path -LiteralPath $checksum) { Remove-Item -LiteralPath $checksum -Force }
    Compress-Archive -LiteralPath $bundleDirectory -DestinationPath $archive -CompressionLevel Optimal
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($archive))" | Set-Content -LiteralPath $checksum -Encoding ascii
    Write-Output $archive
    Write-Output $checksum
}
finally {
    $systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $resolvedTemporary = [IO.Path]::GetFullPath($temporaryRoot)
    if ($resolvedTemporary.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
