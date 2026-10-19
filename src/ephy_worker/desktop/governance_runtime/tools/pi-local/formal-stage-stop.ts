import { appendFileSync } from "node:fs";

// Pi catches extension exceptions. A native exit cannot be swallowed by its
// request loop. This terminates only the process loading this managed extension.
export function stopManagedStage(reason: string): never {
  try {
    appendFileSync(process.env.EPHY_FORMAL_TRACE!, JSON.stringify({
      at: new Date().toISOString(), kind: "hard_stop", reason, exit_code: 78,
    }) + "\n");
  } finally {
    // Even unavailable evidence storage must not permit another provider call.
    process.exit(78);
  }
}
