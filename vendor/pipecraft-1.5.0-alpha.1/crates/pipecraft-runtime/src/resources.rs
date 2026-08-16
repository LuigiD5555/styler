//! Global named-resource coordination shared by every pipeline in a runtime.
//!
//! Resource names are opaque strings. PipeCraft does not know what `dpkg`,
//! `gpu`, `database:migrations`, or `local_llm` mean; clients define that
//! vocabulary. Exclusive users conflict with every other user of the same
//! resource while shared users may coexist with other shared users.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};

use pipecraft_core::model::PipelineStep;
use tokio::sync::Notify;

#[derive(Debug, Default)]
struct ResourceState {
    exclusive: BTreeSet<String>,
    shared: BTreeMap<String, usize>,
}

#[derive(Clone, Debug, Default)]
pub struct GlobalResourceManager {
    state: Arc<Mutex<ResourceState>>,
    notify: Arc<Notify>,
    generation: Arc<AtomicU64>,
}

impl GlobalResourceManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn try_acquire(&self, step: &PipelineStep) -> Option<ResourceLease> {
        let mut state = self.state.lock().expect("resource state poisoned");
        for resource in &step.exclusive_resources {
            if state.exclusive.contains(resource)
                || state.shared.get(resource).copied().unwrap_or(0) > 0
            {
                return None;
            }
        }
        for resource in &step.shared_resources {
            if state.exclusive.contains(resource) {
                return None;
            }
        }

        for resource in &step.exclusive_resources {
            state.exclusive.insert(resource.clone());
        }
        for resource in &step.shared_resources {
            *state.shared.entry(resource.clone()).or_insert(0) += 1;
        }

        Some(ResourceLease {
            manager: self.clone(),
            exclusive: step.exclusive_resources.clone(),
            shared: step.shared_resources.clone(),
            released: false,
        })
    }

    /// Monotonic generation used to avoid lost wakeups between a failed
    /// acquisition and registering a waiter.
    pub fn generation(&self) -> u64 {
        self.generation.load(Ordering::Acquire)
    }

    pub async fn changed_since(&self, observed: u64) {
        loop {
            if self.generation() != observed {
                return;
            }
            let notified = self.notify.notified();
            if self.generation() != observed {
                return;
            }
            notified.await;
        }
    }

    fn release(&self, exclusive: &[String], shared: &[String]) {
        {
            let mut state = self.state.lock().expect("resource state poisoned");
            for resource in exclusive {
                state.exclusive.remove(resource);
            }
            for resource in shared {
                let remove = if let Some(count) = state.shared.get_mut(resource) {
                    *count = count.saturating_sub(1);
                    *count == 0
                } else {
                    false
                };
                if remove {
                    state.shared.remove(resource);
                }
            }
        }
        self.generation.fetch_add(1, Ordering::AcqRel);
        self.notify.notify_waiters();
    }
}

#[derive(Debug)]
pub struct ResourceLease {
    manager: GlobalResourceManager,
    exclusive: Vec<String>,
    shared: Vec<String>,
    released: bool,
}

impl ResourceLease {
    pub fn release(mut self) {
        if !self.released {
            self.manager.release(&self.exclusive, &self.shared);
            self.released = true;
        }
    }
}

impl Drop for ResourceLease {
    fn drop(&mut self) {
        if !self.released {
            self.manager.release(&self.exclusive, &self.shared);
            self.released = true;
        }
    }
}
