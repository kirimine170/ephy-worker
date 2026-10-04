param(
  [Parameter(Mandatory = $true)][string]$WorktreePath,
  [Parameter(Mandatory = $true)][string]$BaseRevision,
  [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-GitCapture {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)

  # Windows PowerShell 5 converts native stderr into ErrorRecord objects.
  # Git writes harmless progress and warnings there, so decide success from
  # the native exit code instead of ErrorActionPreference.
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $output = @(& git @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }
  return [pscustomobject]@{
    Output = [object[]]$output
    ExitCode = [int]$exitCode
  }
}

if (-not (Test-Path -LiteralPath $WorktreePath -PathType Container)) {
  throw "Git worktree not found: $WorktreePath"
}
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
  throw "Artifact output directory not found: $OutputDirectory"
}

$resolvedWorktree = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $WorktreePath).Path)
$resolvedOutput = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $OutputDirectory).Path)
$worktreePrefix = $resolvedWorktree.TrimEnd(
  [IO.Path]::DirectorySeparatorChar,
  [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar
if (
  $resolvedOutput.Equals($resolvedWorktree, [StringComparison]::OrdinalIgnoreCase) -or
  $resolvedOutput.StartsWith($worktreePrefix, [StringComparison]::OrdinalIgnoreCase)
) {
  throw "Artifact output directory must be outside the Git worktree: $resolvedOutput"
}

$statusResult = Invoke-GitCapture @("--no-optional-locks", "-C", $resolvedWorktree, "status", "--short", "--untracked-files=all")
if ($statusResult.ExitCode -ne 0) {
  throw "git status failed with exit code $($statusResult.ExitCode)"
}
[IO.File]::WriteAllLines(
  (Join-Path $OutputDirectory "git-status.txt"),
  [string[]]$statusResult.Output,
  [Text.UTF8Encoding]::new($false)
)

$patchPath = Join-Path $OutputDirectory "changes.patch"
$diffCheckLog = Join-Path $OutputDirectory "verify-diff-check.log"
$resultPath = Join-Path $OutputDirectory "git-artifacts.json"
$temporaryIndex = Join-Path $resolvedOutput (".git-artifact-index-" + [Guid]::NewGuid().ToString("N"))
$temporaryIndexLock = "$temporaryIndex.lock"
$previousIndex = [Environment]::GetEnvironmentVariable("GIT_INDEX_FILE", "Process")
$snapshotLog = [Collections.Generic.List[string]]::new()
$diffCheckExit = $null

try {
  # Seed an isolated index from the immutable submission base, then stage the
  # current worktree snapshot into it. This captures staged, unstaged, deleted,
  # renamed, untracked, and binary content without touching the real index.
  $env:GIT_INDEX_FILE = $temporaryIndex

  $readTreeResult = Invoke-GitCapture @("-C", $resolvedWorktree, "read-tree", $BaseRevision)
  foreach ($line in $readTreeResult.Output) { $snapshotLog.Add([string]$line) }
  if ($readTreeResult.ExitCode -ne 0) {
    throw "git read-tree failed with exit code $($readTreeResult.ExitCode)"
  }

  $addResult = Invoke-GitCapture @("-C", $resolvedWorktree, "add", "--all", "--", ".")
  foreach ($line in $addResult.Output) { $snapshotLog.Add([string]$line) }
  if ($addResult.ExitCode -ne 0) {
    throw "git add to temporary index failed with exit code $($addResult.ExitCode)"
  }

  $patchResult = Invoke-GitCapture @(
    "-C", $resolvedWorktree,
    "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--find-renames",
    "--src-prefix=a/", "--dst-prefix=b/", "--output=$patchPath", $BaseRevision, "--"
  )
  foreach ($line in $patchResult.Output) { $snapshotLog.Add([string]$line) }
  if ($patchResult.ExitCode -ne 0) {
    throw "git diff artifact generation failed with exit code $($patchResult.ExitCode)"
  }

  $diffCheckResult = Invoke-GitCapture @(
    "-C", $resolvedWorktree,
    "diff", "--cached", "--check", "--no-ext-diff", "--no-textconv", $BaseRevision, "--"
  )
  $diffCheckExit = $diffCheckResult.ExitCode
  [IO.File]::WriteAllLines(
    $diffCheckLog,
    [string[]]$diffCheckResult.Output,
    [Text.UTF8Encoding]::new($false)
  )
} finally {
  [IO.File]::WriteAllLines(
    (Join-Path $OutputDirectory "git-snapshot-index.log"),
    [string[]]$snapshotLog,
    [Text.UTF8Encoding]::new($false)
  )
  if ($null -eq $previousIndex) {
    Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
  } else {
    [Environment]::SetEnvironmentVariable("GIT_INDEX_FILE", $previousIndex, "Process")
  }
  foreach ($temporaryPath in @($temporaryIndex, $temporaryIndexLock)) {
    if (Test-Path -LiteralPath $temporaryPath) {
      Remove-Item -LiteralPath $temporaryPath -Force
    }
  }
}

if ($null -eq $diffCheckExit) {
  throw "Git diff check did not run"
}

$result = [ordered]@{
  schemaVersion = 1
  baseRevision = $BaseRevision
  generatedAt = [DateTime]::UtcNow.ToString("o")
  diffCheckExitCode = [int]$diffCheckExit
  patchBytes = (Get-Item -LiteralPath $patchPath).Length
}
[IO.File]::WriteAllText(
  $resultPath,
  "$(($result | ConvertTo-Json -Depth 4))`n",
  [Text.UTF8Encoding]::new($false)
)
