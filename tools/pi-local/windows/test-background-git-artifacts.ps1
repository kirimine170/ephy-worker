param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Git {
  param(
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string[]]$Arguments
  )
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $output = @(& git -C $WorkingDirectory @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }
  if ($exitCode -ne 0) {
    throw "git $($Arguments -join ' ') failed in $WorkingDirectory`n$($output -join "`n")"
  }
  return $output
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactWriter = Join-Path $scriptRoot "write-git-artifacts.ps1"
if (-not (Test-Path -LiteralPath $artifactWriter -PathType Leaf)) {
  throw "Git artifact writer not found: $artifactWriter"
}

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$testRoot = [IO.Path]::GetFullPath((Join-Path $temporaryRoot ("ephy-git-artifacts-" + [Guid]::NewGuid().ToString("N"))))
if (-not $testRoot.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing to use a test directory outside the system temporary directory: $testRoot"
}

$repo = Join-Path $testRoot "repo"
$source = Join-Path $testRoot "source"
$target = Join-Path $testRoot "target"
$artifacts = Join-Path $testRoot "artifacts"
$utf8 = [Text.UTF8Encoding]::new($false)

try {
  New-Item -ItemType Directory -Path $repo, $artifacts -Force | Out-Null
  Invoke-Git $repo @("init", "--initial-branch=main") | Out-Null
  Invoke-Git $repo @("config", "user.name", "Ephy Git Artifact Test") | Out-Null
  Invoke-Git $repo @("config", "user.email", "ephy-git-artifact-test@example.invalid") | Out-Null

  [IO.File]::WriteAllText((Join-Path $repo "tracked.txt"), "before`n", $utf8)
  [IO.File]::WriteAllText((Join-Path $repo "delete-me.txt"), "delete me`n", $utf8)
  [IO.File]::WriteAllText((Join-Path $repo ".gitignore"), "ignored.bin`n", $utf8)
  Invoke-Git $repo @("add", "--", ".") | Out-Null
  Invoke-Git $repo @("commit", "-m", "base") | Out-Null
  $baseRevision = (Invoke-Git $repo @("rev-parse", "HEAD") | Select-Object -First 1).Trim()

  Invoke-Git $repo @("worktree", "add", "--detach", $source, $baseRevision) | Out-Null
  Invoke-Git $repo @("worktree", "add", "--detach", $target, $baseRevision) | Out-Null

  [IO.File]::WriteAllText((Join-Path $source "tracked.txt"), "after`n", $utf8)
  Invoke-Git $source @("add", "--", "tracked.txt") | Out-Null
  Remove-Item -LiteralPath (Join-Path $source "delete-me.txt")

  $skillPath = Join-Path $source ".agents\skills\git-artifact-smoke\SKILL.md"
  New-Item -ItemType Directory -Path (Split-Path -Parent $skillPath) -Force | Out-Null
  $skillContent = "---`nname: git-artifact-smoke`ndescription: Temporary fixture for Git artifact validation.`n---`n`nPreserve this new file in the generated patch.`n"
  [IO.File]::WriteAllText($skillPath, $skillContent, $utf8)
  [IO.File]::WriteAllBytes((Join-Path $source "new-binary.bin"), [byte[]](0, 1, 2, 255, 10))
  [IO.File]::WriteAllBytes((Join-Path $source "ignored.bin"), [byte[]](9, 8, 7))

  $statusBefore = @(Invoke-Git $source @("status", "--porcelain=v1", "--untracked-files=all"))
  $indexBefore = @(Invoke-Git $source @("diff", "--cached", "--binary", $baseRevision, "--"))
  $gitIndexEnvironmentBefore = [Environment]::GetEnvironmentVariable("GIT_INDEX_FILE", "Process")
  & $artifactWriter -WorktreePath $source -BaseRevision $baseRevision -OutputDirectory $artifacts
  $gitIndexEnvironmentAfter = [Environment]::GetEnvironmentVariable("GIT_INDEX_FILE", "Process")
  if ($gitIndexEnvironmentAfter -ne $gitIndexEnvironmentBefore) {
    throw "Artifact writer did not restore GIT_INDEX_FILE"
  }
  $statusAfter = @(Invoke-Git $source @("status", "--porcelain=v1", "--untracked-files=all"))
  $indexAfter = @(Invoke-Git $source @("diff", "--cached", "--binary", $baseRevision, "--"))
  if (($statusAfter -join "`n") -cne ($statusBefore -join "`n")) {
    throw "Artifact writer changed the source worktree status"
  }
  if (($indexAfter -join "`n") -cne ($indexBefore -join "`n")) {
    throw "Artifact writer changed the source worktree index"
  }
  if (@(Get-ChildItem -LiteralPath $artifacts -Force | Where-Object { $_.Name -like ".git-artifact-index-*" }).Count -ne 0) {
    throw "Artifact writer left a temporary Git index behind"
  }

  $patchPath = Join-Path $artifacts "changes.patch"
  if (-not (Test-Path -LiteralPath $patchPath -PathType Leaf) -or (Get-Item -LiteralPath $patchPath).Length -eq 0) {
    throw "Generated patch is missing or empty"
  }
  $artifactResult = Get-Content -Raw -LiteralPath (Join-Path $artifacts "git-artifacts.json") | ConvertFrom-Json
  if ([int]$artifactResult.diffCheckExitCode -ne 0) {
    throw "Clean fixture unexpectedly failed git diff --check"
  }

  $patchText = [IO.File]::ReadAllText($patchPath)
  foreach ($required in @(
    ".agents/skills/git-artifact-smoke/SKILL.md",
    "new-binary.bin",
    "tracked.txt",
    "delete-me.txt",
    "GIT binary patch"
  )) {
    if ($patchText.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
      throw "Generated patch is missing expected content: $required"
    }
  }
  if ($patchText.IndexOf("ignored.bin", [StringComparison]::Ordinal) -ge 0) {
    throw "Generated patch unexpectedly contains an ignored file"
  }

  Invoke-Git $target @("apply", "--check", $patchPath) | Out-Null
  Invoke-Git $target @("apply", $patchPath) | Out-Null

  if ([IO.File]::ReadAllText((Join-Path $target "tracked.txt")) -ne "after`n") {
    throw "Staged tracked change did not survive patch application"
  }
  if (Test-Path -LiteralPath (Join-Path $target "delete-me.txt")) {
    throw "Tracked deletion did not survive patch application"
  }
  if ([IO.File]::ReadAllText((Join-Path $target ".agents\skills\git-artifact-smoke\SKILL.md")) -ne $skillContent) {
    throw "Untracked skill file did not survive patch application"
  }
  $binary = [IO.File]::ReadAllBytes((Join-Path $target "new-binary.bin"))
  $expectedBinary = [byte[]](0, 1, 2, 255, 10)
  $binaryMatches = $binary.Length -eq $expectedBinary.Length
  if ($binaryMatches) {
    for ($byteIndex = 0; $byteIndex -lt $expectedBinary.Length; $byteIndex++) {
      if ($binary[$byteIndex] -ne $expectedBinary[$byteIndex]) {
        $binaryMatches = $false
        break
      }
    }
  }
  if (-not $binaryMatches) {
    throw "Untracked binary file did not survive patch application"
  }
  if (Test-Path -LiteralPath (Join-Path $target "ignored.bin")) {
    throw "Ignored file was created by patch application"
  }

  # The whitespace check must inspect untracked files through the same snapshot.
  [IO.File]::WriteAllText((Join-Path $source "bad-whitespace.txt"), "trailing whitespace  `n", $utf8)
  & $artifactWriter -WorktreePath $source -BaseRevision $baseRevision -OutputDirectory $artifacts
  $badResult = Get-Content -Raw -LiteralPath (Join-Path $artifacts "git-artifacts.json") | ConvertFrom-Json
  if ([int]$badResult.diffCheckExitCode -eq 0) {
    throw "git diff --check did not reject whitespace in an untracked file"
  }
  $diffCheckText = [IO.File]::ReadAllText((Join-Path $artifacts "verify-diff-check.log"))
  if ($diffCheckText.IndexOf("bad-whitespace.txt", [StringComparison]::Ordinal) -lt 0) {
    throw "Whitespace failure log did not identify the untracked file"
  }
  if (($indexAfter -join "`n") -cne (@(Invoke-Git $source @("diff", "--cached", "--binary", $baseRevision, "--")) -join "`n")) {
    throw "Artifact writer changed the source index while checking whitespace"
  }

  Write-Host "PASS: complete patch round-trip, index isolation, and untracked whitespace checking"
} finally {
  if (Test-Path -LiteralPath $repo -PathType Container) {
    if (Test-Path -LiteralPath $source) {
      & git -C $repo worktree remove --force $source *> $null
    }
    if (Test-Path -LiteralPath $target) {
      & git -C $repo worktree remove --force $target *> $null
    }
  }
  if (Test-Path -LiteralPath $testRoot) {
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolvedTestRoot.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
      throw "Refusing to remove a test directory outside the system temporary directory: $resolvedTestRoot"
    }
    Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
  }
}
