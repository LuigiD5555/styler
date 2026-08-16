# PipeCraft Recipes

Recipes are examples of how the same generic runtime can be applied to different
situations. They are not the identity or boundary of PipeCraft.

The product identity is:

> If the problem can be modeled as a pipeline, PipeCraft should help make it
> executable, repeatable, observable, and easier to maintain.

## Recipe pattern

Every recipe should explain:

1. the situation;
2. why it can be represented as a pipeline;
3. the step graph;
4. the relevant policies;
5. the artifacts worth keeping;
6. which parts are generic core features;
7. which parts are domain-specific plugins or scripts.

## Generic process recipe

```text
prepare -> transform -> validate -> approve -> publish
```

This shape can represent releases, document conversion, data movement, local
maintenance, AI evaluation, or many custom internal flows.

## CI/CD-style recipe

A CI/CD recipe may use:

- `command` for build/test tools;
- `git_diff` for read-only repository status;
- `file_check` or `boundary_check` for policy validation;
- `manual_approval` before publishing;
- artifacts for logs, binaries, or reports.

This is a recipe because PipeCraft is not a CI/CD product. It is a pipeline
runtime that can express CI/CD-like flows.

## Browser automation / scraper recipe

A browser automation recipe may use:

- `plugin` to call Python, Node, Playwright, Selenium, or another browser tool;
- `timeout` and `retries` to handle fragile remote pages;
- artifacts for screenshots, downloaded files, HTML snapshots, and parsed data;
- `--from` or `--only` to rerun only the failing section.

This is a recipe because PipeCraft is not a Selenium framework. It coordinates
steps and collects evidence.

## Data processing recipe

A data processing recipe may use:

- `command` for CLI tools;
- `plugin` for Python/Rust/Node transformations;
- `file_check` for required files;
- artifacts for cleaned datasets, validation reports, or rejected rows.

## AI/RAG recipe

An AI/RAG recipe may use:

- `plugin` to call LangChain, LangGraph, custom Python, or local model tooling;
- artifacts for prompts, retrieved context, evaluation results, and traces;
- `manual_approval` before publishing generated outputs.

PipeCraft should orchestrate and report. The AI framework remains outside the
core.

## System maintenance recipe

A system maintenance recipe may use:

- `command` for local tools;
- `file_check` for safety boundaries;
- `copy_or_sync` for backups or sync operations;
- approvals for destructive steps;
- reports for auditability.

PipeCraft is not an operating system service manager. It can still express local
maintenance runbooks.

## n8n-like automation recipe

A visual UI could let users connect blocks and generate PipeCraft YAML. This
could resemble n8n for some app/workflow automation cases.

The distinction:

- n8n-style automation is a possible surface;
- PipeCraft's core remains a general pipeline runtime;
- YAML remains the source of truth.

## Adding recipes

When adding examples, prefer this layout:

```text
examples/
  01-hello-world/
  02-generic-validation/
  recipes/
    ci-cd/
    browser-automation/
    data-processing/
    ai-rag/
    system-maintenance/
    app-automation/
```

Existing numbered examples can remain, but documentation should describe them as
recipes rather than product boundaries.
