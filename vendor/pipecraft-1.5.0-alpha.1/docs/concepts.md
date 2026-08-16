# PipeCraft Concepts

PipeCraft is an adaptive pipeline runtime. Its goal is not to know every domain.
Its goal is to provide a stable way to represent, validate, execute, observe,
and evolve processes that can be modeled as pipelines.

## Problem-to-pipeline thinking

A problem is a good fit for PipeCraft when it can be described as:

```text
start with context/input
perform one or more steps
respect dependencies
apply policies
produce artifacts
produce a report
```

The domain can be anything:

- software delivery;
- document processing;
- local maintenance;
- browser automation;
- data movement;
- AI workflow evaluation;
- operational runbooks;
- custom internal tools.

PipeCraft does not need built-in knowledge of the domain. It only needs the
process to be expressible through pipeline primitives.

## Core primitives

### Pipeline

A named process stored as YAML. It contains metadata, steps, optional context,
optional repos, outputs, and error policy.

### Step

A unit of work. A step can be descriptive (`note`, `checklist`), read-only
(`file_check`, `git_diff`), gated (`manual_approval`), side-effecting
(`command`, `copy_or_sync`), or external (`plugin`).

### Dependency

A `needs` relationship. Dependencies form a DAG. PipeCraft resolves a stable
topological order for validation/tie-breaking and uses dependency state to drive
actual execution. Independent ready branches may run concurrently.

### Run condition

`run_if` controls what predecessor outcome is required: `all_success`,
`all_complete`, `any_failed`, or `always`.

### Resource

An opaque concurrency name. `exclusive_resources` and `shared_resources` let the
runtime coordinate scarce or unsafe-to-overlap facilities without understanding
their domain meaning.

### Capability

Opaque `requires` / `provides` strings express runtime readiness beyond a direct
DAG edge.

### Barrier

A node with `barrier: true` waits for running work and executes alone.

### Executor

The implementation behind a step type. The core includes generic executors.
Specialized executors should be delivered as plugins or separate integrations.

### Policy

Execution behavior such as:

- dry-run vs execute;
- approval requirement;
- timeout;
- retries;
- retry delay;
- error handling.

### Artifact

Any evidence or output produced during a run: files, logs, screenshots, parsed
outputs, copied-file lists, findings, summaries, or plugin-generated data.

### Report

The structured record of a run. Reports should answer:

- what was planned;
- what ran;
- what was skipped;
- what failed;
- what artifacts were produced;
- how long it took;
- what command/plugin/policy was involved.

### Plugin

A bridge to anything PipeCraft does not natively understand. Plugins keep the
core agnostic while allowing domain-specific behavior.

## What belongs in the core

The core should contain concepts that are useful across many domains:

```text
YAML loading
schema validation
routing
DAG planning
execution context
error policy
retries
timeouts and inactivity timeouts
DAG scheduling
run conditions
resources and barriers
capabilities
durable run state / resume
structured events
logs
artifacts
reports
plugin protocol
```

## What belongs outside the core

The following should be plugins, recipes, or external scripts:

```text
Selenium logic
browser selectors
LangChain chains
Odoo operations
Docker-specific deployment policy
cloud provider APIs
business app connectors
custom data transforms
OS-specific maintenance behavior
```

## The adaptation boundary

PipeCraft adapts by letting users change:

- the YAML structure;
- the step graph;
- the executor type;
- the plugin called;
- the policies applied;
- the artifacts captured;
- the reports consumed.

The core should not adapt by becoming domain-specific. It adapts by allowing
domain logic to plug into a generic pipeline model.

## Rule of thumb

Ask:

> Can this situation be represented as steps with dependencies and policies?

If yes, PipeCraft can probably help.

Ask also:

> Does the core need to understand this domain?

If no, implement it as a plugin, script, or recipe instead of expanding the core.
