[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Python,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,

    [Parameter(Mandatory = $true)]
    [string]$KarteRendererBinary,

    [Parameter(Mandatory = $true)]
    [string]$KarteRendererProvenance,

    [Parameter(Mandatory = $true)]
    [string]$GoExecutable,

    [Parameter(Mandatory = $true)]
    [string]$OfficialAppDirectory,

    [Parameter(Mandatory = $true)]
    [string]$OfficialAppManifest,

    [Parameter(Mandatory = $true)]
    [string]$OfficialAppProvenance,

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

# Fixed upstream application files are data for PyInstaller, not another helper.
# Validate the complete downloaded inventory before packaging and again afterward.
$officialAppSource = (Resolve-Path -LiteralPath $OfficialAppDirectory).Path
$officialAppManifestFile = (Resolve-Path -LiteralPath $OfficialAppManifest).Path
$officialAppProvenanceFile = (Resolve-Path -LiteralPath $OfficialAppProvenance).Path
if ([System.IO.Path]::GetFileName($officialAppProvenanceFile) -cne 'provenance.json') {
    throw 'OfficialAppProvenance must be named provenance.json.'
}
$officialAppValidationScript = @'
import hashlib, json, os, stat, sys
from pathlib import Path, PurePosixPath
sys.dont_write_bytecode = True
MANIFEST_SHA256 = '888b469391f59622b5c588d6d26e185fbe70e15dc5d05a58b2ab91da1bea957c'

def require(condition, message):
    if not condition:
        raise ValueError(message)

def plain_path(path, *, file=False):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        value = part.lstat()
        require(not stat.S_ISLNK(value.st_mode) and not
                (getattr(value, 'st_file_attributes', 0) & 0x400), 'Linked or reparse app input')
    value = path.stat()
    if file:
        require(stat.S_ISREG(value.st_mode) and value.st_nlink == 1, 'Non-regular or hardlinked app input')
    else:
        require(stat.S_ISDIR(value.st_mode), 'App directory required')
    return path

def digest_file(path):
    path = plain_path(path, file=True)
    before = path.stat()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            'App input changed while hashing')
    plain_path(path, file=True)
    return after.st_size, digest

def read_manifest(path):
    path = plain_path(path, file=True)
    require(path.stat().st_size <= 128 * 1024, 'Oversized official manifest')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256, 'Official inventory identity mismatch')
    entries = json.loads(raw)
    require(type(entries) is list and len(entries) == 20, 'Official inventory must contain 20 files')
    return entries

def validate_tree(root, entries):
    root = plain_path(root)
    expected = {}
    directories = set()
    for entry in entries:
        require(type(entry) is dict and set(entry) == {'path', 'bytes', 'sha256'}, 'Invalid inventory fields')
        name = entry['path']
        require(type(name) is str and name and '\\' not in name and ':' not in name, 'Invalid inventory path')
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and name == relative.as_posix() and
                not ({'.', '..'} & set(relative.parts)), 'Unsafe inventory path')
        require(name.casefold() not in {key.casefold() for key in expected}, 'Duplicate inventory path')
        require(type(entry['bytes']) is int and entry['bytes'] >= 0 and
                type(entry['sha256']) is str and len(entry['sha256']) == 64 and
                all(char in '0123456789abcdef' for char in entry['sha256']), 'Invalid inventory identity')
        expected[name] = entry
        directories.update(parent.as_posix() for parent in relative.parents if parent.as_posix() != '.')
    seen = set()
    for directory, child_dirs, files in os.walk(root, followlinks=False):
        for name in child_dirs:
            path = Path(directory) / name
            plain_path(path)
            require(path.relative_to(root).as_posix() in directories, 'Unexpected app directory')
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            require(relative in expected, 'Unexpected app file')
            size, digest = digest_file(path)
            require(size == expected[relative]['bytes'] and digest == expected[relative]['sha256'],
                    'Official app file identity mismatch: ' + relative)
            seen.add(relative)
    require(seen == set(expected), 'Official app file missing')

def main(arguments):
    require(not sys.flags.optimize, 'Python optimization must be disabled for inherited build checks')
    source, directory, manifest, provenance = arguments[:4]
    if len(arguments) > 4:
        sys.path.insert(0, arguments[4])
    sys.path.insert(0, source)
    entries = read_manifest(manifest)
    validate_tree(directory, entries)
    provenance_path = plain_path(provenance, file=True)
    require(provenance_path.stat().st_size <= 32 * 1024, 'Oversized official provenance')
    from ephy_worker.desktop.native_karte import validate_official_app
    value = validate_official_app(Path(directory), provenance_path)
    _, provenance_hash = digest_file(provenance_path)
    return {'manifest_sha256': MANIFEST_SHA256, 'provenance_sha256': provenance_hash,
            'file_count': len(entries), 'provenance': value}

if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1:])))
'@
function Test-OfficialApp {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$ProvenanceFile
    )
    $arguments = @('-c', $officialAppValidationScript, (Join-Path $repository 'src'),
        $Directory, $officialAppManifestFile, $ProvenanceFile)
    if ($DependencyPath) { $arguments += $dependencyDirectory }
    $result = & $pythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Pinned official Karte application validation failed.' }
    return (($result -join "`n") | ConvertFrom-Json)
}
$officialApp = Test-OfficialApp -Directory $officialAppSource -ProvenanceFile $officialAppProvenanceFile

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

# Verify the separate read-only helper without executing it or changing the
# existing configured worklog writer. The caller supplies frozen provenance.
$rendererBinary = (Resolve-Path -LiteralPath $KarteRendererBinary).Path
$rendererProvenanceFile = (Resolve-Path -LiteralPath $KarteRendererProvenance).Path
$goPath = (Resolve-Path -LiteralPath $GoExecutable).Path
if ([System.IO.Path]::GetFileName($rendererBinary) -cne 'karte-worklog-render.exe') {
    throw 'KarteRendererBinary must be named karte-worklog-render.exe.'
}
if ([System.IO.Path]::GetFileName($rendererProvenanceFile) -cne 'provenance.json') {
    throw 'KarteRendererProvenance must be named provenance.json.'
}
$rendererValidationScript = @'
import hashlib, json, sys
from pathlib import Path
sys.dont_write_bytecode = True
if len(sys.argv) > 4:
    sys.path.insert(0, sys.argv[4])
sys.path.insert(0, sys.argv[1])
from ephy_worker.desktop.renderer import (
    RENDERER_COMMIT, KARTE_BASE_COMMIT, GO_VERSION, _PROVENANCE_FIELDS, _digest,
)
from ephy_worker.desktop.inspection import _json
binary, source = Path(sys.argv[2]), Path(sys.argv[3])
assert binary.is_file() and not binary.is_symlink(), 'Invalid helper file'
assert source.is_file() and not source.is_symlink() and source.stat().st_size <= 4096, 'Invalid provenance file'
value = _json(source.read_bytes(), 'invalid_provenance')
assert set(value) == _PROVENANCE_FIELDS, 'Unexpected provenance fields'
assert type(value['schema_version']) is int and value['schema_version'] == 1
assert value['renderer_commit'] == RENDERER_COMMIT
assert value['karte_base_commit'] == KARTE_BASE_COMMIT
assert value['go_version'] == GO_VERSION
assert _digest(value['helper_sha256']) and _digest(value['karte_patch_sha256'])
assert 0 < binary.stat().st_size <= 64 * 1024 * 1024
with binary.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == value['helper_sha256'], 'Helper hash mismatch'
print(json.dumps(value))
'@
$rendererValidationArguments = @('-c', $rendererValidationScript,
    (Join-Path $repository 'src'), $rendererBinary, $rendererProvenanceFile)
if ($DependencyPath) { $rendererValidationArguments += $dependencyDirectory }
$rendererValidation = & $pythonPath @rendererValidationArguments
if ($LASTEXITCODE -ne 0) { throw 'Karte renderer provenance validation failed.' }
$rendererProvenance = ($rendererValidation -join "`n") | ConvertFrom-Json
$rendererProvenanceHash = (Get-FileHash -LiteralPath $rendererProvenanceFile -Algorithm SHA256).Hash.ToLowerInvariant()
$goVersion = & $goPath version
if ($LASTEXITCODE -ne 0 -or ($goVersion -join '').Trim() -cne 'go version go1.27.0 windows/amd64') {
    throw 'Pinned Go 1.27.0 windows/amd64 is required to inspect helper build information.'
}
$rendererBuildInformation = @(& $goPath version -m $rendererBinary)
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Karte helper Go build information.' }
$rendererBuildText = $rendererBuildInformation -join "`n"
if ($rendererBuildText -notmatch '(?m)^.+:\s+go1\.27\.0\s*$' -or
    $rendererBuildText -notmatch '(?m)^\s*dep\s+github\.com/kirimine170/KarteRenderer\s+v0\.0\.0\s*$' -or
    $rendererBuildText -notmatch '(?m)^\s*=>\s+github\.com/kirimine170/Karte_renderer\s+v0\.0\.0-20260812100243-30e5dfbab03f\s+h1:[^\s]+\s*$') {
    throw 'Helper build information does not contain the pinned KarteRenderer dependency.'
}

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
Copy-Item -LiteralPath $rendererProvenanceFile -Destination (Join-Path $evidence 'karte-renderer-provenance.json')
Copy-Item -LiteralPath $officialAppManifestFile -Destination (Join-Path $evidence 'karte-native-file-manifest.json')
Copy-Item -LiteralPath $officialAppProvenanceFile -Destination (Join-Path $evidence 'karte-native-provenance.json')
[System.IO.File]::WriteAllText((Join-Path $evidence 'karte-renderer-go-build-info.txt'), $rendererBuildText, $utf8)
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
    '--add-data', ($governanceRuntime + ';ephy_worker/desktop/governance_runtime'),
    '--add-binary', ($rendererBinary + ';ephy_worker/desktop/karte_renderer'),
    '--add-data', ($rendererProvenanceFile + ';ephy_worker/desktop/karte_renderer'),
    '--add-data', ($officialAppSource + ';ephy_worker/desktop/karte_native/app'),
    '--add-data', ($officialAppProvenanceFile + ';ephy_worker/desktop/karte_native'))
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
$rendererPackagePath = '_internal/ephy_worker/desktop/karte_renderer/karte-worklog-render.exe'
$rendererProvenancePackagePath = '_internal/ephy_worker/desktop/karte_renderer/provenance.json'
foreach ($path in @($rendererBinary, (Join-Path $appDirectory $rendererPackagePath))) {
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $rendererProvenance.helper_sha256) {
        throw 'Karte helper changed during build or differs from the bundled copy.'
    }
}
foreach ($path in @($rendererProvenanceFile, (Join-Path $appDirectory $rendererProvenancePackagePath))) {
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $rendererProvenanceHash) {
        throw 'Karte helper provenance changed during build or differs from the bundled copy.'
    }
}
$officialAppPackagePath = '_internal/ephy_worker/desktop/karte_native/app'
$officialAppProvenancePackagePath = '_internal/ephy_worker/desktop/karte_native/provenance.json'
$officialAppSourceAfter = Test-OfficialApp -Directory $officialAppSource -ProvenanceFile $officialAppProvenanceFile
$officialAppFrozen = Test-OfficialApp -Directory (Join-Path $appDirectory $officialAppPackagePath) `
    -ProvenanceFile (Join-Path $appDirectory $officialAppProvenancePackagePath)
foreach ($validated in @($officialAppSourceAfter, $officialAppFrozen)) {
    if ($validated.manifest_sha256 -cne $officialApp.manifest_sha256 -or
        $validated.provenance_sha256 -cne $officialApp.provenance_sha256 -or
        $validated.file_count -ne $officialApp.file_count) {
        throw 'Official Karte application inputs changed during build or differ from the bundled copy.'
    }
}
foreach ($pair in @(
    @((Join-Path $evidence 'karte-native-file-manifest.json'), $officialApp.manifest_sha256),
    @((Join-Path $evidence 'karte-native-provenance.json'), $officialApp.provenance_sha256))) {
    if ((Get-FileHash -LiteralPath $pair[0] -Algorithm SHA256).Hash.ToLowerInvariant() -cne $pair[1]) {
        throw 'Official Karte application evidence differs from verified build inputs.'
    }
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
    karte_renderer_binary = $rendererPackagePath
    karte_renderer_provenance = $rendererProvenancePackagePath
    karte_renderer_provenance_sha256 = $rendererProvenanceHash
    karte_renderer = $rendererProvenance
    karte_native_app = $officialAppPackagePath
    karte_native_provenance = $officialAppProvenancePackagePath
    karte_native_provenance_sha256 = $officialApp.provenance_sha256
    karte_native_manifest_sha256 = $officialApp.manifest_sha256
    karte_native_file_count = $officialApp.file_count
    karte_native = $officialApp.provenance
    python = $probe.python
    architecture = $probe.architecture
    pyinstaller = $probe.pyinstaller
    tk = $probe.tk
    layout = 'onedir-windowed'
    live_model_tested_by_build = $false
}
[System.IO.File]::WriteAllText((Join-Path $appDirectory 'build-info.json'),
    (ConvertTo-Json -InputObject $buildInfo -Depth 8), $utf8)
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
    (ConvertTo-Json -InputObject $buildInfo -Depth 8), $utf8)
Write-Output "Executable: $executable"
Write-Output "Evidence: $evidence"
Write-Output 'Copy the complete EphyWorkerConsole directory, including _internal, to run it.'
