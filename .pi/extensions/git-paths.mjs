import path from "node:path";

/**
 * Convert the drive-path forms emitted by MSYS2/Cygwin Git into a path that
 * Windows process APIs can use as cwd. Native Windows paths and non-Windows
 * platforms are left in their native form.
 */
export function normalizeGitRoot(value, platform = process.platform) {
	const trimmed = value.trim();
	if (platform !== "win32" || trimmed.length === 0) return trimmed;

	const cygdrive = /^\/cygdrive\/([a-zA-Z])(?:\/(.*))?$/.exec(trimmed);
	if (cygdrive) {
		const suffix = cygdrive[2] ? `\\${cygdrive[2].replaceAll("/", "\\")}` : "\\";
		return `${cygdrive[1].toUpperCase()}:${suffix}`;
	}

	const msysDrive = /^\/([a-zA-Z])(?:\/(.*))?$/.exec(trimmed);
	if (msysDrive) {
		const suffix = msysDrive[2] ? `\\${msysDrive[2].replaceAll("/", "\\")}` : "\\";
		return `${msysDrive[1].toUpperCase()}:${suffix}`;
	}

	return path.win32.normalize(trimmed);
}

function powerShellLiteral(value) {
	return `'${value.replaceAll("'", "''")}'`;
}

function windowsProcessArgument(value) {
	return /\s/.test(value) ? `"${value}"` : value;
}

export function buildDetachedRunnerCommand(shell, runner, jobFile) {
	const runnerArguments = [
		"-NoProfile",
		"-NonInteractive",
		"-ExecutionPolicy",
		"Bypass",
		"-File",
		runner,
		"-JobFile",
		jobFile,
	];
	return (
		`$process = Start-Process -FilePath ${powerShellLiteral(shell)} ` +
		`-ArgumentList @(${runnerArguments.map((value) => powerShellLiteral(windowsProcessArgument(value))).join(", ")}) ` +
		"-PassThru -WindowStyle Hidden; [Console]::Out.Write($process.Id)"
	);
}
