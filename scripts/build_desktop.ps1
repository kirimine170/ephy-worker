[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Python,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,

    [string]$DependencyPath,

    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$BuildId
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$requiredPyInstaller = '6.16.0'
$utf8 = [System.Text.UTF8Encoding]::new($false)
$repository = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$outputPath = [System.IO.Path]::GetFullPath($OutputRoot)
$repoPrefix = $repository.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if ($outputPath.Equals($repository, [System.StringComparison]::OrdinalIgnoreCase) -or
    $outputPath.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputRoot must be outside the source checkout.'
}
if ($DependencyPath) {
    $dependencyDirectory = (Resolve-Path -LiteralPath $DependencyPath).Path
    if (-not (Test-Path -LiteralPath $dependencyDirectory -PathType Container)) {
        throw 'DependencyPath must be an existing site-packages directory.'
    }
}

# The same pinned validator used by live runs checks source and frozen resources.
$governanceRuntime = Join-Path $repository 'src\ephy_worker\desktop\governance_runtime'
$runtimeValidationScript = @'
import hashlib, json, sys
from pathlib import Path
sys.dont_write_bytecode = True
if len(sys.argv) > 3:
    sys.path.insert(0, sys.argv[3])
sys.path.insert(0, sys.argv[1])
from ephy_worker.desktop.governance import validate_runtime
runtime = Path(sys.argv[2])
manifest = validate_runtime(runtime)
print(json.dumps({"manifest": manifest,
                  "manifest_sha256": hashlib.sha256((runtime / "manifest.json").read_bytes()).hexdigest()}))
'@
function Test-GovernanceRuntime {
    param([Parameter(Mandatory = $true)][string]$RuntimeDirectory)
    $validationArguments = @('-c', $runtimeValidationScript,
        (Join-Path $repository 'src'), $RuntimeDirectory)
    if ($DependencyPath) { $validationArguments += $dependencyDirectory }
    $validationOutput = & $pythonPath @validationArguments
    if ($LASTEXITCODE -ne 0) { throw 'Pinned governance runtime validation failed.' }
    return (($validationOutput -join "`n") | ConvertFrom-Json)
}
$governance = Test-GovernanceRuntime -RuntimeDirectory $governanceRuntime

# The caller prepares an isolated interpreter. This script never installs tools.
$probeScript = @'
import json, platform, sys, tkinter
from importlib.metadata import distributions, version
assert sys.platform == "win32", "Build on Windows"
assert (3, 12) <= sys.version_info < (3, 15), "Python 3.12 to 3.14 is required"
if len(sys.argv) > 1:
    sys.path.insert(0, sys.argv[1])
import psutil, pydantic
root = tkinter.Tk()
root.withdraw()
tk_version = root.tk.call("package", "provide", "Tk")
root.destroy()
packages = {}
for dist in distributions():
    name = dist.metadata.get("Name")
    if name and name not in packages:
        packages[name] = dist.version
print(json.dumps({"python": platform.python_version(), "architecture": platform.machine(),
                  "pyinstaller": version("pyinstaller"), "tk": tk_version,
                  "dependencies": dict(sorted(packages.items()))}))
'@
$probeArguments = @('-c', $probeScript)
if ($DependencyPath) { $probeArguments += $dependencyDirectory }
$probeOutput = & $pythonPath @probeArguments
if ($LASTEXITCODE -ne 0) { throw 'Build interpreter preflight failed.' }
$probe = ($probeOutput -join "`n") | ConvertFrom-Json
if ($probe.pyinstaller -ne $requiredPyInstaller) {
    throw "PyInstaller $requiredPyInstaller is required; found $($probe.pyinstaller)."
}
$projectText = [System.IO.File]::ReadAllText((Join-Path $repository 'pyproject.toml'), $utf8)
$versionMatch = [regex]::Match($projectText, '(?m)^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"\s*$')
if (-not $versionMatch.Success) { throw 'Expected a numeric project version in pyproject.toml.' }
$version = $versionMatch.Groups[0].Value.Split('"')[1]
$numericVersion = (($versionMatch.Groups[1..3] | ForEach-Object { $_.Value }) -join ', ') + ', 0'
if (-not $BuildId) { $BuildId = "v$version-" + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') }
$buildRoot = Join-Path $outputPath $BuildId
if (Test-Path -LiteralPath $buildRoot) { throw 'BuildId already exists; choose a new BuildId.' }
$null = New-Item -ItemType Directory -Path $buildRoot
$evidence = Join-Path $buildRoot 'evidence'
$work = Join-Path $buildRoot 'work'
$distribution = Join-Path $buildRoot 'dist'
$null = New-Item -ItemType Directory -Path $evidence, $work, $distribution
[System.IO.File]::WriteAllText((Join-Path $evidence 'dependency-manifest.json'),
    (ConvertTo-Json -InputObject $probe.dependencies -Depth 5), $utf8)
Copy-Item -LiteralPath (Join-Path $governanceRuntime 'manifest.json') `
    -Destination (Join-Path $evidence 'governance-runtime-manifest.json')
$versionFile = Join-Path $work 'windows-version.txt'
$resource = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($numericVersion), prodvers=($numericVersion), mask=0x3f,
                   flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Ephy Worker Console'),
    StringStruct('FileVersion', '$version'),
    StringStruct('InternalName', 'EphyWorkerConsole'),
    StringStruct('OriginalFilename', 'EphyWorkerConsole.exe'),
    StringStruct('ProductName', 'Ephy Worker Console'),
    StringStruct('ProductVersion', '$version')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
[System.IO.File]::WriteAllText($versionFile, $resource, $utf8)

$sourceManifest = @()
$sourceFiles = @(Get-ChildItem -LiteralPath (Join-Path $repository 'src\ephy_worker') -Recurse -File -Force |
    Where-Object { $_.Extension -in '.py', '.ts' })
$sourceFiles += @(Get-ChildItem -LiteralPath $governanceRuntime -Recurse -File -Force)
foreach ($relative in @('pyproject.toml', 'uv.lock', 'scripts/build_desktop.ps1',
        'scripts/desktop_entry.py', 'configs/desktop.example.json', 'docs/desktop.md', 'README.md')) {
    $sourceFiles += Get-Item -LiteralPath (Join-Path $repository $relative)
}
foreach ($source in ($sourceFiles | Sort-Object FullName -Unique)) {
    $relative = $source.FullName.Substring($repository.Length + 1).Replace('\', '/')
    $sourceManifest += [ordered]@{
        path = $relative
        bytes = $source.Length
        sha256 = (Get-FileHash -LiteralPath $source.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$sourceManifestFile = Join-Path $evidence 'source-manifest.json'
[System.IO.File]::WriteAllText($sourceManifestFile,
    (ConvertTo-Json -InputObject $sourceManifest -Depth 5), $utf8)
$sourceHash = (Get-FileHash -LiteralPath $sourceManifestFile -Algorithm SHA256).Hash.ToLowerInvariant()
$revision = & git -C $repository rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the source Git revision.' }
$sourceStatus = @(& git -C $repository status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect source status.' }

$pyInstallerArguments = @('-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
    '--name', 'EphyWorkerConsole', '--distpath', $distribution, '--workpath', $work,
    '--specpath', $work, '--paths', (Join-Path $repository 'src'),
    '--version-file', $versionFile, '--collect-submodules', 'ephy_worker.desktop',
    '--add-data', ((Join-Path $repository 'src\ephy_worker\desktop\navigation_guard.ts') + ';ephy_worker/desktop'),
    '--add-data', ($governanceRuntime + ';ephy_worker/desktop/governance_runtime'))
if ($DependencyPath) { $pyInstallerArguments += @('--paths', $dependencyDirectory) }
$pyInstallerArguments += Join-Path $repository 'scripts\desktop_entry.py'
$logPath = Join-Path $evidence 'pyinstaller.log'
Push-Location -LiteralPath $repository
try {
    # Native stderr contains progress messages, not just failures.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $pythonPath @pyInstallerArguments 2>&1 | ForEach-Object {
        $line = $_.ToString()
        [System.IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, $utf8)
        Write-Host $line
    }
    $buildExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
} finally {
    Pop-Location
}
if ($buildExitCode -ne 0) { throw "PyInstaller failed with exit code $buildExitCode. Evidence: $evidence" }
$appDirectory = Join-Path $distribution 'EphyWorkerConsole'
$executable = Join-Path $appDirectory 'EphyWorkerConsole.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Build output is missing.' }

# Verify inputs did not change while the executable was being assembled.
foreach ($source in $sourceManifest) {
    $currentHash = (Get-FileHash -LiteralPath (Join-Path $repository $source.path) -Algorithm SHA256).Hash
    if ($currentHash.ToLowerInvariant() -ne $source.sha256) {
        throw "Source changed during build: $($source.path). Rebuild with a new BuildId."
    }
}
$sourceGovernance = Test-GovernanceRuntime -RuntimeDirectory $governanceRuntime
$frozenRuntime = Join-Path $appDirectory '_internal\ephy_worker\desktop\governance_runtime'
$frozenGovernance = Test-GovernanceRuntime -RuntimeDirectory $frozenRuntime
if ($sourceGovernance.manifest_sha256 -ne $governance.manifest_sha256 -or
    $frozenGovernance.manifest_sha256 -ne $governance.manifest_sha256) {
    throw 'Governance manifest changed during build or differs from the frozen copy.'
}
$buildInfo = [ordered]@{
    schema_version = 1
    application = 'EphyWorkerConsole'
    version = $version
    build_id = $BuildId
    built_at_utc = [DateTime]::UtcNow.ToString('o')
    source_commit = ($revision -join '').Trim()
    source_has_local_changes = ($sourceStatus.Count -gt 0)
    source_manifest_sha256 = $sourceHash
    governance_manifest = '_internal/ephy_worker/desktop/governance_runtime/manifest.json'
    governance_manifest_sha256 = $governance.manifest_sha256
    governance_file_count = $governance.manifest.files.Count
    python = $probe.python
    architecture = $probe.architecture
    pyinstaller = $probe.pyinstaller
    tk = $probe.tk
    layout = 'onedir-windowed'
    live_model_tested_by_build = $false
}
[System.IO.File]::WriteAllText((Join-Path $appDirectory 'build-info.json'),
    (ConvertTo-Json -InputObject $buildInfo -Depth 5), $utf8)
Copy-Item -LiteralPath (Join-Path $repository 'configs\desktop.example.json') -Destination $appDirectory
Copy-Item -LiteralPath (Join-Path $repository 'docs\desktop.md') -Destination (Join-Path $appDirectory 'README.md')
$artifactManifest = @(Get-ChildItem -LiteralPath $appDirectory -Recurse -File -Force | Sort-Object FullName |
    ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($appDirectory.Length + 1).Replace('\', '/')
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
[System.IO.File]::WriteAllText((Join-Path $evidence 'artifact-manifest.json'),
    (ConvertTo-Json -InputObject $artifactManifest -Depth 5), $utf8)
[System.IO.File]::WriteAllText((Join-Path $evidence 'build-info.json'),
    (ConvertTo-Json -InputObject $buildInfo -Depth 5), $utf8)
Write-Output "Executable: $executable"
Write-Output "Evidence: $evidence"
Write-Output 'Copy the complete EphyWorkerConsole directory, including _internal, to run it.'
