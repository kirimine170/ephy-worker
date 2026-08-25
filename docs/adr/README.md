# Architecture Decision Records

Architecture Decision Records capture decisions whose context and consequences should remain understandable after implementation changes．They distinguish an accepted decision from a proposal or an observation about current behavior．

## When to add an ADR

Create an ADR when a change establishes a durable architecture，data，security，interface，or cross-repository boundary．Small reversible implementation details usually do not need an ADR．

## Process

1. Copy `0000-template.md` to the next four-digit number and a short kebab-case title．
2. Fill every section，including alternatives and related repositories．
3. Use `Proposed` while discussion is open．
4. Change the status to `Accepted` only when the decision is approved．Use `Deprecated` or `Superseded by ADR-NNNN` when later decisions replace it．
5. Commit the ADR with the implementation or with the design change that makes the decision operative．

Valid status labels are `Proposed`，`Accepted`，`Deprecated`，and `Superseded`．An ADR date records the decision or proposal date; it is not a delivery deadline．
