# Migrating from the Python prototype

PipeCraft (Rust) is a rewrite of the original `pipelinecraft` Python prototype.
The YAML model is intentionally compatible, so most pipelines move over with
little or no change. This note lists what stayed the same and what changed.

## Compatible at the YAML level

- **Schema version.** The loader accepts both the legacy integer form
  (`schema_version: 1` or `"1"`) and the canonical string form
  (`schema_version: pipecraft/v1`). New pipelines should use `pipecraft/v1`.
- **Pipeline shape.** `name`, `description`, `context`, `repos`, `steps`,
  `outputs`, and `on_error` carry over unchanged.
- **Steps.** `id`, `type`, `repo`, `needs`, `description`, `requires_approval`,
  `required`, `rules`, and `with` all behave as before.
- **Routing.** `routes.yaml` keeps `include` (all), `any`/`any_of` (at least
  one), and `exclude` (none). Both the nested `when.labels.*` form and the
  top-level fallback form are accepted.
- **Error policy.** `on_error` precedence is identical:
  `steps.<id>` → `types.<type>` → `statuses.<status>` → `default`, with policies
  `stop` / `continue` / `warn`.
- **Reports.** Runs are now written to per-run folders: `.pipelines/runs/<run-id>/report.json`, with sibling `logs/` and `artifacts/` directories.

## What changed

- **Workspace file location (bug fix).** The Python prototype's
  `load_workspace` read `.me/workspace.yaml`, while every example and test used
  `.pipelines/workspace.yaml` — so the workspace config was effectively never
  loaded. Rust reads `.pipelines/workspace.yaml`, matching the documented and
  tested layout. If you relied on the old path, move the file.
- **`command` step is explicit about shell.** Prefer
  `with.argv: ["cmd", "arg", ...]` (runs with no shell — safer, no word
  splitting). The `with.command` + `shell: true` form still works but runs via
  `sh -c` and should be reserved for cases that need shell features.
- **Command/plugin `timeout` is enforced in V1.1.** V1 only validated the field. V1.1 adds process timeout handling, retries, retry delay, and separate stdout/stderr logs for process-based execution.
- **AI/stub steps dropped.** The prototype's experimental AI step stubs are not part of the Rust core. AI workflows should be modeled through the generic `plugin` executor or future domain-specific plugin packages.
- **V1.1 step types.** `git_diff`, `copy_or_sync`, `boundary_check`, and `plugin` register as ordinary executors with no engine change. They are generic primitives, not domain-specific product boundaries — see `docs/v1_1-executors.md`.

## Quick checklist to port a pipeline

1. Set `schema_version: pipecraft/v1`.
2. Ensure the workspace lives in `.pipelines/` (not `.me/`).
3. Convert `command` steps to `with.argv: [...]` where you can.
4. If you used any AI/stub step types, replace them with `plugin`, `command`, or a domain script.
5. Treat CI/CD, scraping, release, or AI examples as recipes. Keep the YAML shape generic where possible.
6. Run `pipecraft validate <pipeline>` and fix anything it flags.
