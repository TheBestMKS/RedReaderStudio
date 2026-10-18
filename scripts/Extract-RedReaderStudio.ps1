$ErrorActionPreference = 'Stop'
$basePath = $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $basePath 'portable-parts.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$destinationPath = Join-Path $basePath $manifest.folder
if (Test-Path -LiteralPath $destinationPath) { throw 'Destination already exists. Move the previous extracted folder first.' }
$archivePath = Join-Path $basePath $manifest.archive
if (Test-Path -LiteralPath $archivePath) { throw 'Combined ZIP already exists. Extract it or move it first.' }
foreach ($part in $manifest.parts) {
    if ([IO.Path]::GetFileName($part.name) -ne $part.name) { throw 'Invalid part filename.' }
    $partPath = Join-Path $basePath $part.name
    Write-Host "Checking $($part.name)"
    if (!(Test-Path -LiteralPath $partPath)) { throw "Missing part: $($part.name)" }
    if ((Get-Item -LiteralPath $partPath).Length -ne $part.size) { throw "Wrong size: $($part.name)" }
    if ((Get-FileHash -LiteralPath $partPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $part.sha256) { throw "Checksum mismatch: $($part.name)" }
}
$targetStream = [IO.File]::Open($archivePath,[IO.FileMode]::CreateNew)
try {
    foreach ($part in $manifest.parts) {
        $inputStream = [IO.File]::OpenRead((Join-Path $basePath $part.name))
        try { $inputStream.CopyTo($targetStream) } finally { $inputStream.Dispose() }
    }
} finally { $targetStream.Dispose() }
Add-Type -AssemblyName System.IO.Compression.FileSystem
Write-Host 'Extracting portable application...'
$nativeTar = Join-Path $env:WINDIR 'System32/tar.exe'
if (Test-Path -LiteralPath $nativeTar) {
    & $nativeTar -xf $archivePath -C $basePath
    if ($LASTEXITCODE -ne 0) { throw "Windows tar extraction failed: $LASTEXITCODE" }
} else {
    [IO.Compression.ZipFile]::ExtractToDirectory($archivePath,$basePath)
}
Write-Host "Ready: $destinationPath\RedReaderStudio.exe"
Write-Host 'The combined ZIP and downloaded parts may be removed after checking the application.'
