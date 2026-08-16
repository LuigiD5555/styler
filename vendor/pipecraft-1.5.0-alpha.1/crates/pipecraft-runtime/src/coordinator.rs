//! Shared concurrency controls for one or many pipelines.

use std::sync::Arc;

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::resources::GlobalResourceManager;

#[derive(Clone, Debug)]
pub struct RuntimeCoordinator {
    task_slots: Arc<Semaphore>,
    pub resources: GlobalResourceManager,
    max_tasks: usize,
}

impl RuntimeCoordinator {
    pub fn new(max_tasks: usize) -> Self {
        let max_tasks = max_tasks.max(1);
        Self {
            task_slots: Arc::new(Semaphore::new(max_tasks)),
            resources: GlobalResourceManager::new(),
            max_tasks,
        }
    }

    pub fn max_tasks(&self) -> usize {
        self.max_tasks
    }

    pub fn try_task_slot(&self) -> Option<OwnedSemaphorePermit> {
        self.task_slots.clone().try_acquire_owned().ok()
    }

    pub async fn wait_for_task_slot(&self) {
        if let Ok(permit) = self.task_slots.acquire().await {
            drop(permit);
        }
    }

    pub async fn wait_for_progress(&self, resource_generation: Option<u64>, task_blocked: bool) {
        match (resource_generation, task_blocked) {
            (Some(generation), true) => {
                tokio::select! {
                    _ = self.resources.changed_since(generation) => {},
                    _ = self.wait_for_task_slot() => {},
                }
            }
            (Some(generation), false) => self.resources.changed_since(generation).await,
            (None, true) => self.wait_for_task_slot().await,
            (None, false) => tokio::task::yield_now().await,
        }
    }
}
