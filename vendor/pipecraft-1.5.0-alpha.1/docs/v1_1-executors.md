# PipeCraft V1.1 Executors and Runtime Additions

V1.1 makes the Rust foundation more practical while keeping the product
agnostic. The additions are not for one domain only; they are generic runtime
capabilities that many pipeline-shaped problems need:

- retries;
- timeouts;
- per-run folders;
- logs;
- artifacts;
- partial reruns;
- read-only inspection;
- guarded side effects;
- external plugin execution.

CI/CD, scraping, release management, data processing, and system maintenance are
all examples of how these primitives can be composed.

## Runtime additions

### Process reliability

`command` and `plugin` support:

```yaml
with:
  argv: ["python", "some_task.py"]
  timeout: 120
  retries: 3
  retry_delay: 5
```

- `timeout` is enforced in seconds.
- `retries` rerun failed or timed-out processes.
- `retry_delay` waits between attempts.
- stdout and stderr are captured separately in the run logs folder.

These features are useful for any external tool, not just scripts, scrapers, or
CI commands.

### Run folders

Reports live under a folder per run:

```text
.pipelines/runs/<run-id>/
  report.json
  logs/
  artifacts/
```

Executors and plugins can write debugging evidence under `artifacts/` and
process logs under `logs/`.

### Partial reruns

```sh
pipecraft run my-pipeline --from parse_files
pipecraft run my-pipeline --only parse_files,validate
```

`--from` starts at a step id in the resolved topological order. `--only` filters
execution to named step ids. This helps when a long pipeline partially failed
and the user wants to rerun only the relevant section.

## Built-in executors

The built-ins should stay generic. They are low-level primitives that can support
many recipes.

### `boundary_check`

A generic rule guard. It scans for forbidden paths and terms, and writes a
`findings.txt` artifact.

```yaml
- id: no_private_markers
  type: boundary_check
  rules:
    forbidden_paths:
      - "**/private/**"
    forbidden_terms:
      - "INTERNAL-ONLY"
```

Possible uses:

- public/private package boundaries;
- safety checks before publishing;
- generated-output validation;
- policy checks in local automation.

### `git_diff`

Read-only Git inspection. Supported modes:

```yaml
with:
  mode: status      # git status --short
  # mode: stat      # git diff --stat
  # mode: name-only # git diff --name-only
  # mode: cached    # git diff --cached --stat
```

### `copy_or_sync`

Copies files from `source` to `destination` with optional glob filters. `sync`
currently behaves as overwrite-copy; it does not delete extra destination files.

```yaml
- id: export_files
  type: copy_or_sync
  requires_approval: true
  with:
    source: .
    destination: ../exported-output
    include:
      - "src/**"
      - "README.md"
    exclude:
      - "**/.env*"
      - "**/private/**"
```

Dry-run reports the planned file list. Execute mode copies files and writes a
`copied-files.txt` artifact.

### `plugin`

The first polyglot bridge. PipeCraft launches any executable and sends this JSON
payload on stdin:

```json
{
  "protocol": "pipecraft.plugin/v1",
  "run_id": "...",
  "pipeline": "...",
  "step_id": "...",
  "root": "...",
  "cwd": "...",
  "labels": [],
  "dry_run": false,
  "with": {},
  "context": {},
  "artifacts_dir": "...",
  "logs_dir": "..."
}
```

A plugin may return JSON on stdout:

```json
{
  "success": true,
  "status": "ok",
  "message": "done",
  "output": "human-readable output",
  "data": {"records": 10}
}
```

If stdout is not JSON, PipeCraft falls back to command-like result handling.

## Executor design guideline

Add to the core only when the behavior is broadly useful across domains. If a
behavior depends on a specific ecosystem, vendor, framework, or business domain,
prefer a plugin or recipe.

Examples:

| behavior | recommended home |
|---|---|
| run a process | core `command` |
| capture plugin JSON | core `plugin` |
| inspect Git status | core `git_diff` |
| copy files with filters | core `copy_or_sync` |
| Selenium browser control | plugin |
| LangGraph invocation | plugin |
| Odoo import details | plugin or recipe |
| cloud provider API calls | plugin |
| app connector workflows | plugin or UI layer |
