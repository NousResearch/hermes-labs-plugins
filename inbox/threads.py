from __future__ import annotations

_SAFE_FIELDS = set("edge_id run_id origin_profile target_profile trace_id parent_edge_id origin_session_id status delivery delivery_state dispatched_at updated_at completed_at observed_in source_conflict dispatch_type hop_count max_hops".split())

def _text(value) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def build_threads(edges: list[dict]) -> list[dict]:
    if not isinstance(edges, list):
        return []
    # Deduplicate identity (origin_profile, edge_id)
    identity_items: dict[tuple[str, str], list[dict]] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        origin = _text(e.get("origin_profile", ""))
        edge_id = _text(e.get("edge_id", ""))
        key = (origin, edge_id)
        identity_items.setdefault(key, []).append(e)
    # Build nodes with conflict detection
    nodes: list[dict] = []
    for key, group in identity_items.items():
        canonical = group[0]
        node = dict(canonical)
        # Check conflicts (different values besides identity keys)
        conflict = False
        reference = dict(canonical)
        for g in group[1:]:
            for k, v in reference.items():
                if k in ("origin_profile", "edge_id", "observed_in"):
                    continue
                if g.get(k) != reference.get(k):
                    conflict = True
        node["_conflict"] = conflict
        nodes.append(node)
    # Union-find by non-blank trace and parent links
    parent = {}
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri
    n = len(nodes)
    parent = {i: i for i in range(n)}
    # Index nodes by edge_id for parent lookup
    node_by_edge_id: dict[str, list[int]] = {}
    for i, node in enumerate(nodes):
        eid = _text(node.get("edge_id", ""))
        node_by_edge_id.setdefault(eid, []).append(i)
    # Union by exact nonblank trace
    trace_groups: dict[str, list[int]] = {}
    for i, node in enumerate(nodes):
        trace = _text(node.get("trace_id", ""))
        if trace:
            trace_groups.setdefault(trace, []).append(i)
    for group in trace_groups.values():
        first = group[0]
        for other in group[1:]:
            union(first, other)
    # Parent links: resolve exactly one; ambiguous -> unresolved, no union.
    unresolved_by_node: dict[int, set[str]] = {}
    for i, node in enumerate(nodes):
        p_edge = _text(node.get("parent_edge_id", ""))
        if p_edge:
            candidates = node_by_edge_id.get(p_edge, [])
            if len(candidates) == 0:
                unresolved_by_node.setdefault(i, set()).add(p_edge)
            elif len(candidates) == 1:
                union(i, candidates[0])
            else:
                # Ambiguous: resolve by trace if possible
                trace = _text(node.get("trace_id", ""))
                if trace:
                    filtered = [c for c in candidates if _text(nodes[c].get("trace_id", "")) == trace]
                else:
                    filtered = candidates
                if len(filtered) == 1:
                    union(i, filtered[0])
                else:
                    unresolved_by_node.setdefault(i, set()).add(p_edge)
    # Build components
    components: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        components.setdefault(root, []).append(i)
    # Compute threads
    thread_list = []
    for root, members in components.items():
        component_nodes = [nodes[i] for i in members]
        # Determine unique nonblank traces
        traces = sorted({_text(node.get("trace_id", "")) for node in component_nodes if _text(node.get("trace_id", ""))})
        origin_profiles = sorted({_text(node.get("origin_profile", "")) for node in component_nodes})
        targets = sorted({_text(node.get("target_profile", "")) for node in component_nodes if _text(node.get("target_profile", ""))})
        # Stable canonical node for id
        canonical_key = min((_text(node.get("origin_profile", "")), _text(node.get("edge_id", ""))) for node in component_nodes)
        if len(traces) == 1 and traces[0] != "":
            thread_id = f"trace:{traces[0]}"
        else:
            thread_id = f"edge:{canonical_key[0]}:{canonical_key[1]}"
        # Edge ids qualified
        edge_ids = sorted({f"{_text(node.get('origin_profile',''))}:{_text(node.get('edge_id',''))}" for node in component_nodes})
        # Source conflict flag
        source_conflict = any(node.get("_conflict") or node.get("source_conflict") for node in component_nodes)
        # Execution state
        statuses = [_text(node.get("status", "")) for node in component_nodes]
        running_states = ("dispatched", "running", "finalizing")
        has_running = any(s in running_states for s in statuses)
        failed_states = ("error", "failed")
        has_failed = any(s in failed_states for s in statuses)
        completed_states = ("completed",)
        all_completed = all(s in completed_states for s in statuses) if statuses else False
        all_cancelled = all(s == "cancelled" for s in statuses) if statuses else False
        if source_conflict:
            execution = "unknown"
        elif has_running:
            execution = "running"
        elif has_failed:
            execution = "failed"
        elif all_completed:
            execution = "completed"
        elif all_cancelled:
            execution = "cancelled"
        else:
            execution = "unknown"
        # Delivery: unknown unless explicit actual delivered state present
        # Only treat actual 'delivered' receipt state; never infer from completed.
        deliveries = sorted({_text(node.get("delivery", "")) for node in component_nodes})
        # Preserve modes list but default unknown
        delivery = ["delivered"] if all(node.get("delivery_state") == "delivered" for node in component_nodes) else ["unknown"]
        # Build safe edges (strip internal keys)
        safe_edges = []
        for node in component_nodes:
            safe = {k: v for k, v in node.items() if k in _SAFE_FIELDS}
            safe_edges.append(safe)
        safe_edges.sort(key=lambda e: (e.get("origin_profile", ""), e.get("edge_id", "")))
        unresolved_parents_sorted = sorted({ref for i in members for ref in unresolved_by_node.get(i, set())})
        thread = {
            "id": thread_id,
            "edge_ids": edge_ids,
            "origins": origin_profiles,
            "targets": targets,
            "edges": safe_edges,
            "execution": execution,
            "outcome": "unknown",
            "evidence": [],
            "delivery": delivery,
            "delivery_modes": deliveries,
            "unresolved_parents": unresolved_parents_sorted,
            "source_conflict": source_conflict,
        }
        thread_list.append(thread)
    # Sort deterministically
    thread_list.sort(key=lambda t: t["id"])
    return thread_list
