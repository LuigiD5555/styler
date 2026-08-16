//! `pipecraft-runtime` — execution context, the step executor registry, the built-in executors, Tokio scheduler, global resource manager and
//! multi-pipeline runtime manager.

pub mod context;
pub mod coordinator;
pub mod engine;
pub mod executor;
pub mod events;
pub mod manager;
pub mod process;
pub mod resources;
pub mod scheduler;
pub mod steps;

pub use context::{CancellationToken, ExecutionContext};
pub use coordinator::RuntimeCoordinator;
pub use engine::PipelineEngine;
pub use executor::{ExecutorRegistry, RepositoryResolver, StepExecutor, StepFuture};
pub use manager::{PipelineRequest, RuntimeLimits, RuntimeManager};
pub use resources::GlobalResourceManager;
pub use scheduler::RunState;

#[cfg(test)]
mod tests {
    use super::*;
    use pipecraft_core::PipelineCatalog;
    use std::path::Path;

    const WORKSPACE: &str = r#"
schema_version: pipecraft/v1
workspace:
  name: default
paths:
  pipeline_dir: .pipelines/pipelines
  route_file: .pipelines/routes.yaml
  runs_dir: .pipelines/runs
defaults:
  default_pipeline: demo-pipeline
"#;

    const PIPELINE: &str = r#"
schema_version: pipecraft/v1
name: demo-pipeline
repos:
  current:
    path: .
steps:
  - id: intent
    type: note
    description: Capture intent.
  - id: review
    type: checklist
    needs: [intent]
    with:
      items:
        - Review the diff
        - Update docs if needed
  - id: safety
    type: file_check
    repo: current
    needs: [intent]
    rules:
      forbidden_paths: ["**/.env*"]
  - id: gate
    type: manual_approval
    needs: [review, safety]
    requires_approval: true
    required: false
on_error:
  default: stop
"#;

    fn setup(dir: &Path) {
        let pdir = dir.join(".pipelines").join("pipelines");
        std::fs::create_dir_all(&pdir).unwrap();
        std::fs::write(dir.join(".pipelines/workspace.yaml"), WORKSPACE).unwrap();
        std::fs::write(pdir.join("demo-pipeline.yaml"), PIPELINE).unwrap();
    }

    #[test]
    fn validates_clean_pipeline() {
        let dir = tempfile::tempdir().unwrap();
        setup(dir.path());
        let pipeline = PipelineCatalog::open(dir.path()).unwrap().load("demo-pipeline").unwrap();
        assert_eq!(PipelineEngine::default().validate(&pipeline), Vec::<String>::new());
    }

    #[test]
    fn topological_order_respects_needs() {
        let dir = tempfile::tempdir().unwrap();
        setup(dir.path());
        let pipeline = PipelineCatalog::open(dir.path()).unwrap().load("demo-pipeline").unwrap();
        let order = PipelineEngine::default().plan(&pipeline).unwrap();
        // intent must come before review/safety, which come before gate.
        let pos = |id: &str| order.iter().position(|x| x == id).unwrap();
        assert!(pos("intent") < pos("review"));
        assert!(pos("intent") < pos("safety"));
        assert!(pos("review") < pos("gate"));
        assert!(pos("safety") < pos("gate"));
    }

    #[test]
    fn dry_run_marks_gate_needs_approval() {
        let dir = tempfile::tempdir().unwrap();
        setup(dir.path());
        let pipeline = PipelineCatalog::open(dir.path()).unwrap().load("demo-pipeline").unwrap();
        let ctx = ExecutionContext::new(dir.path().to_path_buf())
            .labels(vec!["demo".into()])
            .dry_run(true);
        let run = PipelineEngine::default().run(&pipeline, &ctx);
        assert!(run.success); // gate is required: false, so it doesn't fail the run
        assert!(run.results.iter().any(|r| r.status == "needs_approval"));
    }
}
