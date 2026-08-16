//! `pipecraft-graph` — dependency DAG with stable topological ordering and
//! cycle detection.
//!
//! This crate is intentionally tiny and dependency-free. It knows nothing about
//! steps, executors, YAML or commands — only nodes (by string id) and their
//! `needs` edges. The runtime feeds it step ids and gets back either a valid
//! execution order or a coded cycle error.
//!
//! The topological sort is a *stable* Kahn's algorithm: among nodes that are
//! ready at the same time, the original insertion order is preserved. That means
//! a pipeline with no `needs` at all runs in exactly its file order, matching the
//! Python prototype's behaviour, while still being a real DAG underneath ready
//! for parallel scheduling in V2.

use std::collections::{BTreeMap, VecDeque};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphError {
    /// One or more dependency cycles exist. Contains the node ids still involved
    /// in cycles after as many nodes as possible were ordered.
    Cycle { remaining: Vec<String> },
}

impl std::fmt::Display for GraphError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GraphError::Cycle { remaining } => {
                write!(f, "dependency cycle detected among steps: {}", remaining.join(", "))
            }
        }
    }
}

impl std::error::Error for GraphError {}

/// A node id and the ids it depends on.
pub struct Node {
    pub id: String,
    pub needs: Vec<String>,
}

/// Compute a stable topological order from `(id, needs)` pairs, preserving the
/// input order for nodes that become ready simultaneously.
///
/// Dangling `needs` (pointing at unknown ids) are ignored here — that is a
/// validation concern surfaced earlier with a clearer message. This keeps the
/// graph layer purely about ordering and cycles.
pub fn topological_order(nodes: &[Node]) -> Result<Vec<String>, GraphError> {
    // Preserve declaration order via an index map.
    let mut order_index: BTreeMap<&str, usize> = BTreeMap::new();
    for (i, n) in nodes.iter().enumerate() {
        order_index.insert(n.id.as_str(), i);
    }

    let known: std::collections::HashSet<&str> = nodes.iter().map(|n| n.id.as_str()).collect();

    // Build in-degree (only counting edges to known nodes) and adjacency.
    let mut indegree: BTreeMap<&str, usize> = nodes.iter().map(|n| (n.id.as_str(), 0)).collect();
    let mut dependents: BTreeMap<&str, Vec<&str>> = BTreeMap::new();

    for n in nodes {
        for dep in &n.needs {
            let dep = dep.as_str();
            if known.contains(dep) {
                *indegree.get_mut(n.id.as_str()).unwrap() += 1;
                dependents.entry(dep).or_default().push(n.id.as_str());
            }
        }
    }

    // Seed the queue with ready nodes, in declaration order.
    let mut ready: Vec<&str> = nodes
        .iter()
        .map(|n| n.id.as_str())
        .filter(|id| indegree[id] == 0)
        .collect();
    ready.sort_by_key(|id| order_index[id]);
    let mut queue: VecDeque<&str> = ready.into_iter().collect();

    let mut ordered: Vec<String> = Vec::with_capacity(nodes.len());

    while let Some(id) = queue.pop_front() {
        ordered.push(id.to_string());
        if let Some(children) = dependents.get(id) {
            // Collect newly-ready children, then enqueue them in declaration order
            // to keep the sort stable.
            let mut newly_ready: Vec<&str> = Vec::new();
            for &child in children {
                let d = indegree.get_mut(child).unwrap();
                *d -= 1;
                if *d == 0 {
                    newly_ready.push(child);
                }
            }
            newly_ready.sort_by_key(|id| order_index[id]);
            for child in newly_ready {
                queue.push_back(child);
            }
        }
    }

    if ordered.len() != nodes.len() {
        let mut remaining: Vec<String> = nodes
            .iter()
            .map(|n| n.id.clone())
            .filter(|id| !ordered.contains(id))
            .collect();
        remaining.sort();
        return Err(GraphError::Cycle { remaining });
    }

    Ok(ordered)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(id: &str, needs: &[&str]) -> Node {
        Node { id: id.into(), needs: needs.iter().map(|s| s.to_string()).collect() }
    }

    #[test]
    fn preserves_file_order_without_needs() {
        let nodes = vec![node("a", &[]), node("b", &[]), node("c", &[])];
        assert_eq!(topological_order(&nodes).unwrap(), vec!["a", "b", "c"]);
    }

    #[test]
    fn orders_by_dependencies() {
        let nodes = vec![node("package", &["test"]), node("test", &["lint"]), node("lint", &[])];
        assert_eq!(topological_order(&nodes).unwrap(), vec!["lint", "test", "package"]);
    }

    #[test]
    fn detects_cycle() {
        let nodes = vec![node("a", &["b"]), node("b", &["a"])];
        let err = topological_order(&nodes).unwrap_err();
        assert_eq!(err, GraphError::Cycle { remaining: vec!["a".into(), "b".into()] });
    }
}
