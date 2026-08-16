# PipeCraft

**PipeCraft is an adaptive, local-first, polyglot pipeline runtime driven by YAML.**

If a problem can be expressed as **steps, dependencies, inputs, outputs,
policies, artifacts, and reports**, PipeCraft should help you make that process
explicit, repeatable, observable, and easier to evolve.

PipeCraft is not tied to CI/CD, scraping, AI, Odoo, release automation, or
n8n-style app automation. Those are useful **recipes**. The product identity is
more general:

> PipeCraft turns step-by-step technical processes into executable, reviewable,
> versionable pipelines.

The YAML is the source of truth — not a hosted control plane, not an opaque UI
state, not a database-only workflow definition. A future UI can help users create
YAML, visualize DAGs, and inspect reports, but the pipeline contract remains
plain files that can live in Git.

> Status: `1.5.0-alpha.1`. PipeCraft remains **Rust-first**, but the Rust
> runtime can now stay resident as a local IPC service. Clients submit jobs and
> reconnect by `run_id`; the service persists job intent separately from DAG
> state and uses the same Tokio scheduler/resource coordinator across independent
> submissions. Python is a thin stdlib-only IPC client, not a second runtime.
> The `pipecraft/v1` YAML schema remains compatible.

## v1.5 resident runtime service

PipeCraft 1.5 adds an optional long-lived execution boundary without requiring a
hosted control plane:

```text
CLI / Python / Styler / other clients
             │
      pipecraft.ipc/v1
             │
             ▼
    PipeCraft Rust service
             │
   shared Tokio coordinator
      ┌──────┼──────┐
      ▼      ▼      ▼
    job A  job B  job C
```

On Unix, the default endpoint is `<workspace>/.pipelines/pipecraft.sock` and is
created with user-only permissions. Start the service with:

```sh
pipecraft --root . serve --max-pipelines 8 --max-tasks 32
```

Submit work without tying execution to the client process:

```sh
pipecraft --root . submit build --execute --max-workers 4
pipecraft --root . status <run-id>
pipecraft --root . job-events <run-id>
pipecraft --root . cancel <run-id>
pipecraft --root . resume-job <run-id>
pipecraft --root . job-report <run-id>
```

The old one-shot `run` and `run-many` paths remain available. A runtime service
is optional, not a requirement for using PipeCraft.

Persistence is now split deliberately:

```text
.pipelines/runtime/jobs/<run-id>.json   # durable service intent/lifecycle
.pipelines/runs/<run-id>/state.json     # resumable DAG state
.pipelines/runs/<run-id>/events.jsonl   # append-only observations
.pipelines/runs/<run-id>/report.json    # latest attempt report
.pipelines/runs/<run-id>/pipeline.snapshot.yaml # immutable submitted definition
.pipelines/runs/<run-id>/attempts/        # preserved attempt reports
```

Submitted jobs snapshot their validated pipeline YAML before acknowledgement, so later source-tree edits cannot silently change queued work. After an unclean service restart, previously queued/running jobs become
`interrupted`. Manual recovery is the safe default. Resume reuses only durably
confirmed successful nodes whose fingerprint still matches; an interrupted node
may be replayed, so exactly-once external effects remain the responsibility of
idempotent/domain-aware executors.

See `docs/runtime-service-v1.5.md`, `docs/persistence-v1.5.md`, and
`docs/changelog/CHANGELOG_v1_5_0.md`.


## The core idea

Many workflows look different on the surface:

- run tests and publish a build;
- download files from a web portal;
- convert documents through OCR;
- synchronize folders;
- clean a system directory;
- run an AI/RAG evaluation;
- validate that a public package does not contain private files;
- connect app-like automations behind a visual UI.

But underneath, many of them can be reduced to the same shape:

```text
input/context
  -> step A
  -> step B and step C
  -> validation
  -> optional approval
  -> side effect
  -> artifacts/report
```

PipeCraft focuses on that shared structure.

## What PipeCraft is

PipeCraft is a **runtime for pipelines**, not a domain-specific automation app.

It provides generic primitives:

| primitive | meaning |
|---|---|
| pipeline | named process represented as YAML |
| step | one executable or descriptive unit of work |
| dependency | `needs` relationship between steps |
| condition | `run_if` rule evaluated after dependencies complete |
| resource | opaque exclusive/shared concurrency constraint |
| capability | opaque `requires` / `provides` readiness contract |
| barrier | node that executes alone |
| executor | implementation behind a step type |
| policy | timeout, retries, error handling, approval |
| artifact | evidence/output created by a run or step |
| report | structured record of what happened |
| plugin | bridge to an external tool, language, or domain |

Everything domain-specific should live either in YAML, external scripts, or
plugins. The core should remain small and agnostic.

## What PipeCraft is not

PipeCraft should avoid becoming a giant domain tool. It is not primarily:

- an n8n clone;
- an Airflow replacement;
- a GitHub Actions replacement;
- a Selenium framework;
- a LangChain/LangGraph framework;
- a systemd replacement;
- an Odoo-specific tool.

It may integrate with tools in those spaces, but the core should only know how
to validate, plan, run, observe, and report pipelines.

## Why

- **Adaptive** — the same runtime can model different situations as long as the
  situation can be expressed as a pipeline.
- **Local-first** — no server, account, hosted control plane, or telemetry is
  required.
- **YAML-first** — pipelines are plain, reviewable, diff-able files. Git can be
  the history.
- **Dry-run first** — a `run` simulates by default. Side-effecting steps require
  `--execute`; gated steps additionally require `--approve`.
- **Polyglot** — Rust, Python, Node, Bash, Docker, HTTP clients, AI tools, and
  internal binaries can all participate as executors or plugins.
- **Observable** — each run writes a structured report, resumable state, an
  append-only JSONL event stream, logs, and artifacts under
  `.pipelines/runs/<run-id>/`.

## Layout

A workspace is any directory containing a `.pipelines/` folder:

```text
.pipelines/
  workspace.yaml            # workspace name + defaults
  routes.yaml               # map labels/intents -> a pipeline
  pipelines/
    my-pipeline.yaml        # one file per pipeline
  runtime/
    jobs/                   # durable resident-service job records
  pipecraft.sock            # Unix IPC socket while service is running
  runs/                     # one folder per run:
                            # report.json, state.json, events.jsonl, logs/, artifacts/
```

## Install / Build

Requires a stable Rust toolchain (`rustc` / `cargo`, edition 2021).

```sh
cargo build --release
cargo test
```

The binary is created at:

```text
target/release/pipecraft
```

## Commands

```sh
pipecraft --root <dir> list
pipecraft --root <dir> validate <pipeline>
pipecraft --root <dir> plan <pipeline>
pipecraft --root <dir> route <labels>
pipecraft --root <dir> run <pipeline> [--labels "..."] [--execute] [--approve] [--from <step>] [--only a,b] [--max-workers N] [--run-id ID] [--resume]
pipecraft --root <dir> run-many <pipeline>... [--execute] [--approve] [--worker-threads N] [--max-pipelines N] [--max-tasks N] [--max-workers N]
pipecraft --root <dir> run-labels "<labels>" [--execute] [--approve] [--from <step>] [--only a,b] [--max-workers N] [--run-id ID] [--resume]
```

`--root` defaults to the current directory. Labels can be hash tags like `#ci`
or bare tokens like `ci release`. Routing resolves them through
`.pipelines/routes.yaml`.

## Minimal pipeline

```yaml
schema_version: pipecraft/v1
name: hello-process

description: A tiny domain-neutral pipeline.

steps:
  - id: explain
    type: note
    description: Explain what this pipeline is about.
    with:
      message: "This is a repeatable process represented as steps."

  - id: run_tool
    type: command
    needs: [explain]
    with:
      argv: ["echo", "PipeCraft executes tools, not opinions about your domain."]

  - id: review
    type: checklist
    needs: [run_tool]
    with:
      items:
        - "Did the command run?"
        - "Is the report useful?"
```

Run it as a dry-run first:

```sh
pipecraft run hello-process
```

Execute side effects explicitly:

```sh
pipecraft run hello-process --execute
```

## Step types in V1.1 alpha

These built-ins are intentionally generic. Specialized behavior should be added
through plugins or recipes.

| type | side effects | role |
|---|:---:|---|
| `note` | no | Add human-readable context to the report. |
| `checklist` | no | Capture manual review items. |
| `command` | yes | Run a local command using `argv` or shell command text. |
| `file_check` | no | Scan files for forbidden paths or terms. |
| `manual_approval` | gate | Require `--approve` before continuing. |
| `target_plan` | no | Describe a target operation without touching it. |
| `boundary_check` | no | Generic guard for boundaries, leakage, or rule violations. |
| `git_diff` | no | Read-only Git status/diff summary. |
| `copy_or_sync` | yes | Copy/sync files with include/exclude rules. |
| `plugin` | yes | Run any external language/tool through JSON stdin/stdout. |

`command`, `copy_or_sync`, and `plugin` are dry-run unless `--execute` is
passed. Steps marked `requires_approval: true` additionally need `--approve`.

V1.3 keeps enforced `timeout`, `retries`, `retry_delay`, per-step logs,
artifact folders, `--from`, and `--only`, and adds `inactivity_timeout`,
`run_if`, resources, capabilities, barriers, events, resume, and opt-in DAG
parallelism.

## Recipes, not identity

The examples demonstrate how generic primitives can be applied. They are not the
limits of PipeCraft.

| example | recipe demonstrated |
|---|---|
| `examples/01-hello-world` | minimal process: note → command → checklist |
| `examples/02-ci-build-test-deploy` | CI/CD-style validation and gated deploy |
| `examples/03-maintenance-scraper` | browser/scraper-shaped maintenance flow |
| `examples/04-release-management` | release checklist and gated publishing |
| `examples/07-open-core-boundaries` | boundary guard for public/private outputs |
| `examples/08-concurrent-dag` | concurrent branches, capabilities, resources and barrier |
| `examples/09-multi-pipeline-global-resources` | two pipelines sharing one global exclusive resource |

More recipe categories can be added without changing the core:

```text
recipes/data-processing/
recipes/ai-rag/
recipes/system-maintenance/
recipes/document-processing/
recipes/app-automation/
recipes/os-tasks/
recipes/custom-domain/
```

## Extension model

PipeCraft should grow through a small core and specialized extensions:

```text
PipeCraft Core
  -> validates YAML
  -> builds execution plans
  -> schedules the executable DAG
  -> coordinates conditions/resources/capabilities
  -> applies policies
  -> persists state and emits events
  -> records logs, artifacts, reports

Plugins / integrations
  -> Python
  -> Node
  -> Docker
  -> Selenium
  -> LangChain / LangGraph
  -> Odoo
  -> HTTP
  -> any custom binary
```

The built-in `plugin` executor is the first bridge: PipeCraft sends JSON to an
external process through stdin and reads JSON back from stdout. This keeps the
core language-agnostic.

## Routing

`routes.yaml` maps labels to pipelines:

```yaml
schema_version: pipecraft/v1
routes:
  - id: build-check
    when:
      labels:
        any: [ci, build, verify]
    pipeline: build-test-deploy
```

When no route matches, PipeCraft falls back to the workspace `default_pipeline`
with a warning.

## Error policy

Per-pipeline `on_error` decides what happens when a required step fails.
Precedence:

```text
steps.<id>  ->  types.<type>  ->  statuses.<status>  ->  default
```

Each resolves to one of:

```text
stop | continue | warn
```

## Workspace crates

| crate | responsibility |
|---|---|
| `pipecraft-core` | YAML models, loader, routing, static validation. |
| `pipecraft-graph` | Dependency-free DAG and stable topological order. |
| `pipecraft-runtime` | Tokio DAG scheduler, multi-pipeline manager, global resources, state/events, async process runtime, executors. |
| `pipecraft-report` | Run/step result types and JSON report writer. |
| `pipecraft-cli` | The `pipecraft` binary. |

## Python SDK

The [`python/`](python/) package is intentionally thin. It does not contain a
scheduler, process runtime, plugin framework, installer, graph implementation,
or retry logic. It only invokes the Rust CLI's JSON contract and maps reports to
small Python objects.

```python
from pipecraft import PipeCraft

pc = PipeCraft(root=".")
report = pc.run("hello", max_workers=4)
runs = pc.run_many(["build", "tests", "docs"], max_tasks=16)
```

Python plugins do **not** need this package. `type: plugin` is a polyglot process
protocol: read JSON from stdin and write JSON to stdout. See
[`docs/python-bridge.md`](docs/python-bridge.md).

## Design principle

The most important rule:

> Core agnostic, plugins specific, recipes demonstrative, UI optional.

## Verification

The assembly environment did not provide a Rust toolchain or outbound DNS, so the Python client and static package checks were executed here, while Rust compilation still requires a machine with Cargo. Before promoting this alpha, run:

```sh
cargo fmt
cargo test
cargo clippy --all-targets --all-features -- -D warnings
cargo build --release
```

## Further docs

- [`docs/runtime-v1.4.md`](docs/runtime-v1.4.md)
- [`docs/changelog/CHANGELOG_v1_4_0.md`](docs/changelog/CHANGELOG_v1_4_0.md)
- [`docs/concepts.md`](docs/concepts.md)
- [`docs/recipes.md`](docs/recipes.md)
- [`docs/v1_1-executors.md`](docs/v1_1-executors.md)
- [`docs/migration-from-python.md`](docs/migration-from-python.md)
- [`docs/python-bridge.md`](docs/python-bridge.md)
- [`docs/changelog/CHANGELOG_v1_2_1.md`](docs/changelog/CHANGELOG_v1_2_1.md)
- [`docs/changelog/CHANGELOG_v1_2_0.md`](docs/changelog/CHANGELOG_v1_2_0.md)
- [`docs/changelog/CHANGELOG_v1_1_1.md`](docs/changelog/CHANGELOG_v1_1_1.md)

## License

Apache-2.0. See [`LICENSE`](LICENSE).
