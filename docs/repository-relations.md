# Repository relationships

## Source of truth

`.ephy/project.yaml` is the source of truth for the repository's place in the Ephy ecosystem．A repository declares its own parent and only the direct relationships it needs．The future `ephy` meta repository can collect these declarations to build the ecosystem registry and dependency graph．

## Fields

- `relations.parent` identifies the logical parent project．It does not imply Git nesting or a submodule．The ecosystem root meta project uses `null` and every other project uses a project ID．A project cannot be its own parent，and parent cycles are invalid．
- `relations.depends_on` lists repositories whose APIs，artifacts，or behavior are direct requirements．
- `relations.integrates_with` lists direct peers connected through an integration boundary．
- `relations.runs_on` lists direct runtime or platform relationships．

All identifiers use lowercase kebab-case．Arrays must not contain duplicates．Do not add a downstream，children，consumers，or dependents field: those relationships are derived by reversing declarations from other repositories．

## Update rule

Update the metadata in the same change that introduces or removes a direct relationship．If a relationship changes the contract of another repository，coordinate the matching change there without copying its full repository registry into this repository．

## Boundaries

Repository relationships do not authorize access，deployment，release，or data exchange．Those actions require their own explicit configuration and approval．Never distribute an entire private-instance repository to a worker merely because the worker has a runtime relationship with it．
