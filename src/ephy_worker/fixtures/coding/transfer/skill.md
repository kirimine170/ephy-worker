# Boundary-preserving Python repair

Before editing, list the function's stated input domain, output contract, and allowed files.
Check empty input, boundary values, repeated values, and meaningful falsey values.
Distinguish missing from present-but-empty, inclusive from exclusive bounds, and global from adjacent duplicates.
Trace one normal example and one boundary example through the current implementation.
Change only the implementation needed to satisfy the contract. Keep existing tests and unrelated behavior intact.
Run the prescribed checks. If a tool or check is unavailable, report it instead of claiming success.
