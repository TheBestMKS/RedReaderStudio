$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$releaseRoot = Join-Path $repoRoot 'release/0.7.0'
$pythonExe = Join-Path $releaseRoot 'runtime/python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) { throw 'Сначала подготовьте portable Python и зависимости из desktop/requirements-lock.txt.' }
foreach ($modelFile in @('higgs/model.safetensors', 'codec/model.safetensors', 'whisper-small/model.bin', 'piper/ru_RU-irina-medium.onnx', 'piper/ru_RU-denis-medium.onnx', 'piper/ru_RU-dmitri-medium.onnx', 'qwen2.5-1.5b/.complete', 'qwen2.5-3b/.complete')) {
    if (!(Test-Path -LiteralPath (Join-Path $releaseRoot "models/$modelFile"))) { throw "Модель ещё не скачана: $modelFile" }
}
& $pythonExe -c 'import torch, torchaudio, piper, faster_whisper, PySide6, bitsandbytes, accelerate, mutagen'
if ($LASTEXITCODE -ne 0) { throw 'Runtime dependencies are incomplete' }
foreach ($runtimeDll in Get-ChildItem -Path (Join-Path $releaseRoot 'runtime/Lib/site-packages/PySide6/*140*.dll')) {
    $dllTarget = Join-Path $releaseRoot ('runtime/' + $runtimeDll.Name)
    if ((Test-Path -LiteralPath $dllTarget) -and ((Get-FileHash -LiteralPath $dllTarget).Hash -eq (Get-FileHash -LiteralPath $runtimeDll.FullName).Hash)) { continue }
    Copy-Item -LiteralPath $runtimeDll.FullName -Destination $dllTarget -Force
}
New-Item -ItemType Directory -Force (Join-Path $releaseRoot 'app') | Out-Null
Copy-Item -Path (Join-Path $repoRoot 'desktop/*.py') -Destination (Join-Path $releaseRoot 'app') -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'desktop/app.ico') -Destination (Join-Path $releaseRoot 'app') -Force
New-Item -ItemType Directory -Force (Join-Path $releaseRoot 'app/app') | Out-Null
foreach ($name in @('__init__.py', 'document_reader.py', 'chunker.py')) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "backend/app/$name") -Destination (Join-Path $releaseRoot 'app/app') -Force
}
& $pythonExe -m pytest (Join-Path $repoRoot 'desktop') -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$icon = Join-Path $repoRoot 'desktop/app.ico'
& $compiler /nologo /codepage:65001 /target:winexe /reference:System.Windows.Forms.dll "/out:$releaseRoot/RedReaderStudio.exe" "/win32icon:$icon" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher build failed' }
Copy-Item -LiteralPath (Join-Path $repoRoot 'desktop/README_RU.txt') -Destination $releaseRoot -Force
Write-Host "Release ready: $releaseRoot"

Copy-Item -LiteralPath (Join-Path $repoRoot 'desktop/API_RU.md') -Destination $releaseRoot -Force
