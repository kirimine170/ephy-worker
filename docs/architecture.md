# Architecture

## Purpose

This document describes the stable responsibility boundaries of the repository．Project-specific implementations should extend it with components，interfaces，data flows，failure modes，and deployment boundaries．

## Repository layers

- `.ephy/` contains the machine-readable project identity，direct repository relationships，and data policy．
- `docs/` contains architecture，relationship，security，and decision records．
- `.github/` contains repository-local collaboration and validation configuration．
- `scripts/` contains stack-independent repository initialization and validation．
- `tests/` verifies the repository tooling without modifying the source checkout．

## Responsibility boundaries

Each Ephy repository owns a defined project responsibility．Cross-repository relationships must be explicit in `.ephy/project.yaml`，but downstream consumers are discovered centrally by the future `ephy` meta repository rather than copied into every repository．Git submodules are not an architecture model for Ephy relationships．

Implementation-specific source trees，package managers，build systems，and runtime architecture are intentionally absent from this base template．Add them only after the generated repository has selected its technology and responsibility boundaries．

## Implementation state and proposals

Document current behavior as implementation state．Document unaccepted ideas as proposals，and use an ADR when a decision has lasting architectural impact．Do not infer delivery dates or completion percentages from the project status field．
