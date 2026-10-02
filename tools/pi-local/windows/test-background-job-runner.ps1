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
$runner = Join-Path $scriptRoot "run-background-job.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
  throw "Background runner not found: $runner"
}

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$testRoot = [IO.Path]::GetFullPath((Join-Path $temporaryRoot ("ephy background runner " + [Guid]::NewGuid().ToString("N"))))
if (-not $testRoot.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing to use a test directory outside the system temporary directory: $testRoot"
}

$repo = Join-Path $testRoot "repo"
$jobDir = Join-Path $testRoot "job"
$worktree = Join-Path $testRoot "worktrees\runner-smoke"
$stubSourceFile = Join-Path $testRoot "stub-pi.cs"
$stubExecutable = Join-Path $testRoot "stub-pi.exe"
$dummyAgentDir = Join-Path $testRoot "dummy agent"
$dummyExtensionsDir = Join-Path $dummyAgentDir "extensions"
$dummyProviderExtension = Join-Path $dummyExtensionsDir "dual-provider.ts"
$dummySubagentExtension = Join-Path $dummyAgentDir "subagent\index.ts"
$dummyGovernanceGate = Join-Path $dummyExtensionsDir "governance-gate.ts"
$dummyGovernancePolicy = Join-Path $dummyAgentDir "policies\development-governance.md"
$dummyWorkerAgent = Join-Path $dummyAgentDir "agents\qwen-worker.md"
$dummyPrompt = Join-Path $testRoot "dummy-prompt.md"
$jobFile = Join-Path $jobDir "job.json"
$utf8 = [Text.UTF8Encoding]::new($false)
$powershellExecutable = (Get-Command powershell.exe -ErrorAction Stop).Source
$csharpCompiler = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $csharpCompiler -PathType Leaf)) {
  throw "C# compiler not found: $csharpCompiler"
}

$environmentNames = @(
  "DUAL_PI_EXE",
  "DUAL_PROVIDER_EXTENSION",
  "DUAL_SUBAGENT_EXTENSION",
  "DUAL_GOVERNANCE_GATE",
  "DUAL_GOVERNANCE_POLICY",
  "DUAL_GOVERNANCE_ROLE",
  "DUAL_ORCHESTRATOR_PROMPT",
  "DUAL_POWERSHELL_EXE",
  "DUAL_BACKGROUND_CHILD",
  "DUAL_TEST_REPAIR_MODE",
  "PI_CODING_AGENT_DIR"
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
  $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$extraWorktrees = @()

function Invoke-AdditionalSmokeJob {
  param([string]$CaseName, [string]$Mode, [string]$Command, [string]$ExpectedStatus, [int]$ExpectedHistoryCount)
  $caseJobDir = Join-Path $testRoot "job-$CaseName"
  $caseWorktree = Join-Path $testRoot "worktrees\$CaseName"
  $caseJobFile = Join-Path $caseJobDir "job.json"
  New-Item -ItemType Directory -Path $caseJobDir -Force | Out-Null
  $caseJob = [ordered]@{
    schemaVersion = 1
    id = $CaseName
    title = "Background runner $CaseName smoke test"
    status = "queued"
    createdAt = [DateTime]::UtcNow.ToString("o")
    updatedAt = [DateTime]::UtcNow.ToString("o")
    sourceCwd = $repo
    repoRoot = $repo
    baseRevision = $baseRevision
    dirtyAtSubmit = $false
    task = "Test bounded automatic repair"
    doneWhen = @("Verification reaches the expected terminal state")
    verificationCommands = @($Command)
    timeoutMinutes = 5
    maxRepairAttempts = 2
    executionProfile = "dual-local-coding"
    worktreePath = $caseWorktree
    jobDir = $caseJobDir
    verificationResults = @()
  }
  [IO.File]::WriteAllText($caseJobFile, "$(($caseJob | ConvertTo-Json -Depth 12))" + [Environment]::NewLine, $utf8)
  [IO.File]::WriteAllText((Join-Path $caseJobDir "TASK.md"), "Test bounded automatic repair." + [Environment]::NewLine, $utf8)
  $env:DUAL_TEST_REPAIR_MODE = $Mode
  $caseRunnerCommand = "& '$($runner.Replace("'", "''"))' -JobFile '$($caseJobFile.Replace("'", "''"))'"
  $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($caseRunnerCommand))
  $caseProcess = Start-Process -FilePath $powershellExecutable -ArgumentList @("-NoProfile", "-NonInteractive", "-EncodedCommand", $encoded) -WorkingDirectory $scriptRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $caseJobDir "runner.stdout.log") -RedirectStandardError (Join-Path $caseJobDir "runner.stderr.log") -Wait
  $script:extraWorktrees += $caseWorktree
  $caseResult = Get-Content -Raw -LiteralPath $caseJobFile | ConvertFrom-Json
  if ($caseProcess.ExitCode -ne 0 -or $caseResult.status -ne $ExpectedStatus) {
    $runnerError = Get-Content -LiteralPath (Join-Path $caseJobDir "runner-error.log") -Raw -ErrorAction SilentlyContinue
    throw "Case $CaseName failed: process=$($caseProcess.ExitCode), status=$($caseResult.status), message=$($caseResult.message), runnerError=$runnerError"
  }
  $historyCount = if ($caseResult.PSObject.Properties.Name -contains "verificationHistory") {
    @($caseResult.verificationHistory).Count
  } else {
    0
  }
  if ($historyCount -ne $ExpectedHistoryCount) {
    throw "Case $CaseName expected $ExpectedHistoryCount verification attempt(s), got $historyCount"
  }
  return $caseResult
}

try {
  New-Item -ItemType Directory -Path $repo, $jobDir -Force | Out-Null
  Invoke-Git $repo @("init", "--initial-branch=main") | Out-Null
  Invoke-Git $repo @("config", "user.name", "Ephy Background Runner Test") | Out-Null
  Invoke-Git $repo @("config", "user.email", "ephy-background-runner-test@example.invalid") | Out-Null
  [IO.File]::WriteAllText((Join-Path $repo "tracked.txt"), "before`n", $utf8)
  Invoke-Git $repo @("add", "--", ".") | Out-Null
  Invoke-Git $repo @("commit", "-m", "base") | Out-Null
  $baseRevision = (Invoke-Git $repo @("rev-parse", "HEAD") | Select-Object -First 1).Trim()

  $stubSource = @'
using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

internal static class Program
{
    private const string PolicyId = "ephy.system-development-governance.v1";
    private const string EndMarker = "END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1";

    private static string FileSha256(string path)
    {
        using (var stream = File.OpenRead(path))
        using (var sha256 = SHA256.Create())
        {
            var digest = sha256.ComputeHash(stream);
            var text = new StringBuilder(digest.Length * 2);
            foreach (byte value in digest)
                text.Append(value.ToString("x2"));
            return text.ToString();
        }
    }

    private static string AckMessage(string role, string policySha256)
    {
        return "{\"role\":\"toolResult\",\"toolName\":\"governance_ack\",\"details\":{" +
            "\"acknowledged\":true," +
            "\"role\":\"" + role + "\"," +
            "\"policyId\":\"" + PolicyId + "\"," +
            "\"policySha256\":\"" + policySha256 + "\"," +
            "\"endMarker\":\"" + EndMarker + "\"," +
            "\"nonce\":\"stub-" + role + "\"}}";
    }

    private static int Main(string[] args)
    {
        string worktree = Environment.CurrentDirectory;
        var utf8 = new UTF8Encoding(false);
        string mode = Environment.GetEnvironmentVariable("DUAL_TEST_REPAIR_MODE") ?? "";
        string prompt = String.Join(" ", args);
        string content = "changed by agent\n";
        if (mode == "repair")
            content = prompt.Contains("REPAIR-") ? "repaired\n" : "broken\n";
        if (mode == "oscillate")
            content = prompt.Contains("REPAIR-01") ? "state-b\n" : "state-a\n";
        File.WriteAllText(Path.Combine(worktree, "tracked.txt"), content, utf8);
        string skillPath = Path.Combine(worktree, ".agents", "skills", "runner-smoke", "SKILL.md");
        Directory.CreateDirectory(Path.GetDirectoryName(skillPath));
        File.WriteAllText(
            skillPath,
            "---\nname: runner-smoke\ndescription: Runner integration fixture.\n---\n\nCreated by the stub agent.\n",
            utf8
        );
        string policyPath = Environment.GetEnvironmentVariable("DUAL_GOVERNANCE_POLICY");
        string policySha256 = FileSha256(policyPath);
        string leadAck = AckMessage("lead", policySha256);
        string implementerAck = AckMessage("implementer", policySha256);
        Console.WriteLine("{\"type\":\"message_end\",\"message\":" + leadAck + "}");
        Console.WriteLine(
            "{\"type\":\"message_end\",\"message\":{" +
            "\"role\":\"toolResult\",\"toolName\":\"subagent\",\"details\":{" +
            "\"results\":[{\"messages\":[" + implementerAck + "]}]}}}"
        );
        return 0;
    }
}
'@
  [IO.File]::WriteAllText($stubSourceFile, "$stubSource`n", $utf8)
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $compilerOutput = @(& $csharpCompiler /nologo /target:exe "/out:$stubExecutable" $stubSourceFile 2>&1)
    $compilerExit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }
  if ($compilerExit -ne 0) {
    throw "Stub compilation failed with exit code $compilerExit`n$($compilerOutput -join "`n")"
  }
  New-Item -ItemType Directory -Path $dummyExtensionsDir, (Split-Path -Parent $dummySubagentExtension), (Split-Path -Parent $dummyGovernancePolicy), (Split-Path -Parent $dummyWorkerAgent) -Force | Out-Null
  foreach ($dummyExtension in @($dummyProviderExtension, $dummySubagentExtension, $dummyGovernanceGate)) {
    [IO.File]::WriteAllText($dummyExtension, "export default function () {}`n", $utf8)
  }
  [IO.File]::WriteAllText(
    $dummyGovernancePolicy,
    "# Runner smoke governance`n`nephy.system-development-governance.v1`n`nEND-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1`n",
    $utf8
  )
  [IO.File]::WriteAllText(
    $dummyWorkerAgent,
    "---`nname: qwen-worker`ndescription: Runner smoke Qwen worker.`nmodel: dual-local/Qwen3-Coder-Next-Q4_K_M`n---`n`nRunner smoke worker.`n",
    $utf8
  )
  [IO.File]::WriteAllText($dummyPrompt, "Runner smoke fixture.`n", $utf8)
  [IO.File]::WriteAllText((Join-Path $jobDir "TASK.md"), "Create the runner smoke fixture.`n", $utf8)

  $verificationCommand = 'if (([IO.File]::ReadAllText((Join-Path (Get-Location).Path "tracked.txt")).Trim()) -ne "changed by agent") { [Console]::Error.WriteLine("Expected agent state"); exit 1 }'
  $createdAt = [DateTime]::UtcNow.ToString("o")
  $job = [ordered]@{
    schemaVersion = 1
    id = "runner-smoke"
    title = "Background runner smoke test"
    status = "queued"
    createdAt = $createdAt
    updatedAt = $createdAt
    sourceCwd = $repo
    repoRoot = $repo
    baseRevision = $baseRevision
    dirtyAtSubmit = $false
    task = "Create deterministic smoke-test files"
    doneWhen = @("Patch contains agent and verification outputs")
    verificationCommands = @($verificationCommand)
    timeoutMinutes = 5
    executionProfile = "dual-local-coding"
    worktreePath = $worktree
    jobDir = $jobDir
    verificationResults = @()
  }
  [IO.File]::WriteAllText($jobFile, "$(($job | ConvertTo-Json -Depth 12))`n", $utf8)

  $env:DUAL_PI_EXE = $stubExecutable
  $env:DUAL_PROVIDER_EXTENSION = $dummyProviderExtension
  $env:DUAL_SUBAGENT_EXTENSION = $dummySubagentExtension
  $env:DUAL_GOVERNANCE_GATE = $dummyGovernanceGate
  $env:DUAL_GOVERNANCE_POLICY = $dummyGovernancePolicy
  $env:DUAL_ORCHESTRATOR_PROMPT = $dummyPrompt
  $env:DUAL_POWERSHELL_EXE = $powershellExecutable
  Remove-Item Env:DUAL_BACKGROUND_CHILD -ErrorAction SilentlyContinue

  $runnerCommand = "& '$($runner.Replace("'", "''"))' -JobFile '$($jobFile.Replace("'", "''"))'"
  $encodedRunnerCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($runnerCommand))
  $runnerOut = Join-Path $testRoot "runner.stdout.log"
  $runnerErr = Join-Path $testRoot "runner.stderr.log"
  $runnerProcess = Start-Process `
    -FilePath $powershellExecutable `
    -ArgumentList @("-NoProfile", "-NonInteractive", "-EncodedCommand", $encodedRunnerCommand) `
    -WorkingDirectory $scriptRoot `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $runnerOut `
    -RedirectStandardError $runnerErr `
    -Wait
  if ($runnerProcess.ExitCode -ne 0) {
    $stderr = if (Test-Path -LiteralPath $runnerErr) { [IO.File]::ReadAllText($runnerErr) } else { "" }
    $runnerError = if (Test-Path -LiteralPath (Join-Path $jobDir "runner-error.log")) {
      [IO.File]::ReadAllText((Join-Path $jobDir "runner-error.log"))
    } else { "" }
    $jobState = if (Test-Path -LiteralPath $jobFile) { [IO.File]::ReadAllText($jobFile) } else { "" }
    throw "Background runner exited with code $($runnerProcess.ExitCode)`n$stderr`n$runnerError`n$jobState"
  }

  $completedJob = Get-Content -Raw -LiteralPath $jobFile | ConvertFrom-Json
  if ($completedJob.status -ne "audit_pending") {
    throw "Expected audit_pending, got $($completedJob.status): $($completedJob.message)"
  }
  if (@($completedJob.verificationResults).Count -ne 2) {
    throw "Expected one acceptance command and one Git diff check"
  }
  if (@($completedJob.verificationResults | Where-Object { [int]$_.exitCode -ne 0 }).Count -ne 0) {
    throw "A runner verification step failed"
  }

  $patchPath = Join-Path $jobDir "changes.patch"
  $patchText = [IO.File]::ReadAllText($patchPath)
  foreach ($required in @(
    "tracked.txt",
    ".agents/skills/runner-smoke/SKILL.md"
  )) {
    if ($patchText.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
      throw "Final runner patch is missing expected content: $required"
    }
  }

  $repairCommand = 'if (([IO.File]::ReadAllText((Join-Path (Get-Location).Path "tracked.txt")).Trim()) -ne "repaired") { [Console]::Error.WriteLine("Expected repaired state"); exit 1 }'
  $repairResult = Invoke-AdditionalSmokeJob -CaseName "repair-success" -Mode "repair" -Command $repairCommand -ExpectedStatus "audit_pending" -ExpectedHistoryCount 2
  if ($repairResult.message -notmatch "1 repair attempt") {
    throw "Successful repair did not report its attempt count"
  }

  $oscillationCommand = 'if (([IO.File]::ReadAllText((Join-Path (Get-Location).Path "tracked.txt")).Trim()) -ne "never") { [Console]::Error.WriteLine("Expected never state"); exit 1 }'
  $oscillationResult = Invoke-AdditionalSmokeJob -CaseName "repair-oscillation" -Mode "oscillate" -Command $oscillationCommand -ExpectedStatus "verification_failed" -ExpectedHistoryCount 3
  if ($oscillationResult.message -notmatch "Repair loop detected") {
    throw "Oscillating repair was not identified as a loop"
  }

  $badCommand = '[Console]::Error.WriteLine("error: unrecognized arguments: --bogus"); exit 2'
  $badCommandResult = Invoke-AdditionalSmokeJob -CaseName "bad-verification-command" -Mode "repair" -Command $badCommand -ExpectedStatus "verification_failed" -ExpectedHistoryCount 1
  if ($badCommandResult.message -notmatch "verification command has invalid arguments") {
    throw "Invalid verification command was not identified before attempting a repair"
  }

  $mutationCommand = '[IO.File]::WriteAllText((Join-Path (Get-Location).Path "verification-mutation.txt"), "unexpected verification mutation`n", [Text.UTF8Encoding]::new($false))'
  $mutationResult = Invoke-AdditionalSmokeJob -CaseName "verification-mutation" -Mode "" -Command $mutationCommand -ExpectedStatus "verification_failed" -ExpectedHistoryCount 0
  if ($mutationResult.message -notmatch "(?i)verification.*(modified|mutated|changed)|(?:modified|mutated|changed).*verification") {
    throw "Verification mutation was not identified as a contract failure"
  }
  if (@(Get-ChildItem -LiteralPath (Join-Path $testRoot "job-verification-mutation") -Filter "agent-repair-*.jsonl" -ErrorAction SilentlyContinue).Count -ne 0) {
    throw "Verification mutation incorrectly entered candidate repair"
  }

  $infrastructureCommand = '[Console]::Error.WriteLine("ruff is not recognized as the name of a cmdlet"); exit 127'
  $infrastructureResult = Invoke-AdditionalSmokeJob -CaseName "infrastructure-failure" -Mode "repair" -Command $infrastructureCommand -ExpectedStatus "verification_failed" -ExpectedHistoryCount 1
  if ($infrastructureResult.message -notmatch "environment or runner failure") {
    throw "Infrastructure failure was not stopped before candidate repair"
  }

  Invoke-Git $repo @("apply", "--check", $patchPath) | Out-Null
  Invoke-Git $repo @("apply", $patchPath) | Out-Null
  if ([IO.File]::ReadAllText((Join-Path $repo "tracked.txt")) -ne "changed by agent`n") {
    throw "Agent change did not apply"
  }
  if (-not (Test-Path -LiteralPath (Join-Path $repo ".agents\skills\runner-smoke\SKILL.md") -PathType Leaf)) {
    throw "Agent-created skill did not apply"
  }
  Write-Host "PASS: governed background runner artifacts, audit pending, bounded repair, verification-mutation stop, infrastructure stop, and space-path handling"
} finally {
  foreach ($name in $environmentNames) {
    $previousValue = $previousEnvironment[$name]
    if ($null -eq $previousValue) {
      Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    } else {
      [Environment]::SetEnvironmentVariable($name, $previousValue, "Process")
    }
  }
  if ((Test-Path -LiteralPath $repo -PathType Container) -and (Test-Path -LiteralPath $worktree)) {
    & git -C $repo worktree remove --force $worktree *> $null
  }
  foreach ($extraWorktree in $extraWorktrees) {
    if ((Test-Path -LiteralPath $repo -PathType Container) -and (Test-Path -LiteralPath $extraWorktree)) {
      & git -C $repo worktree remove --force $extraWorktree *> $null
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
