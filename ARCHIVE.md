# Historical source archive: governance-and-evaluation

Historical governance and self-improvement evaluation snapshots.

This branch preserves historical source snapshots. They are unverified and are
not adopted. This collector does not approve, implement, release or merge the
experiments into the implementation base.

The collector uses publication base `26ddbe65a9bd9fe530dca13238c2eb3838f3aee5` and adds only
`ARCHIVE.md` and `ARCHIVED-WORKTREES.json`. Its snapshot parents retain the imported
historical source trees. They do not imply adoption.

Each mapping records the relative worktree slug, original HEAD, `origsourceCommit`
and `origsourceTree`, together with the actual `published_snapshot_commit` and
`published_snapshot_tree`. The published source tree must equal `origsourceTree`.

The GitHub connector recreates snapshot commits on the publication base. Their
commit identity, author, timestamp and original history differ from
`origsourceCommit`. The private local Git bundle preserves the original exact
commit history; the private source ZIPs preserve original disk bytes and line
endings. The recreated public commits preserve source tree contents.

Restore a public snapshot from its `published_snapshot_commit` in a separate
checkout, or with `git archive --format=zip --output=snapshot.zip <commit>`.
Use private local ZIPs and their recorded SHA-256 hashes to recover exact original
disk bytes or runtime evidence. Private runtime logs and evidence are excluded
from this public collector metadata.

The entire `encoding-candidate` experiment family is excluded from these public
grouped refs and stays in the private local bundle and ZIPs. Automatic approval
review rejected publication of this local-only unadopted series.

Do not merge these archives into production as feature implementation.
