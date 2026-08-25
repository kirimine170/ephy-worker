# @@PROJECT_ID@@

## Overview

@@PROJECT_DESCRIPTION@@

## Role in the Ephy ecosystem

This repository is an Ephy `@@PROJECT_TYPE@@` project．Its status is `@@PROJECT_STATUS@@` and its intended visibility is `@@PROJECT_VISIBILITY@@`．Repository relationships are declared in `.ephy/project.yaml`．

## Goals

- Describe the outcomes this repository owns．
- Keep responsibilities aligned with its declared Ephy project type．

## Non-goals

- Do not duplicate responsibilities owned by related repositories．
- Do not maintain a downstream repository registry in this repository．

## Current status

The current implementation status is `@@PROJECT_STATUS@@`．This label describes observed implementation state，not a delivery date or completion percentage．

## Architecture

Document components，interfaces，data flows，and responsibility boundaries in [docs/architecture.md](docs/architecture.md)．Record durable decisions under `docs/adr/`．

## Repository relationships

- Parent project: `@@PROJECT_PARENT@@`
- Direct dependencies:
@@DEPENDS_ON_MARKDOWN@@
- Integration peers:
@@INTEGRATES_WITH_MARKDOWN@@
- Runtime platforms:
@@RUNS_ON_MARKDOWN@@

Declare only the parent and direct relationships．Do not list downstream consumers，and do not use Git submodules to represent ecosystem relationships．See [docs/repository-relations.md](docs/repository-relations.md)．

## Getting started

Add project-specific setup instructions here after selecting the implementation stack．

## Testing

Document project-specific test commands here．Keep the repository metadata validation in the standard verification path:

```bash
python3 scripts/validate_repository.py
```

## Security and data handling

The data classification is `@@DATA_CLASSIFICATION@@`．Do not commit secrets，unnecessary personal data，raw conversation history，production Karte data，master camera images，raw LoRA training data，or model weights．See [docs/security-and-data.md](docs/security-and-data.md)．

## Documentation

- [Architecture](docs/architecture.md)
- [Repository relationships](docs/repository-relations.md)
- [Security and data handling](docs/security-and-data.md)
- [Architecture Decision Records](docs/adr/README.md)

## License

No license has been selected automatically．Determine the repository's visibility and license explicitly before distribution，then add the appropriate license file and update this section．
