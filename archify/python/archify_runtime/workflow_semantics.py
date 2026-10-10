"""Directed graph contracts authored for Workflow diagrams."""

from .geometry import as_array


def semantic_contract_diagnostics(workflow):
    checks = workflow.get("semanticChecks")
    if checks is None or checks is False or checks == "":
        return []

    node_ids = list(dict.fromkeys(node.get("id") for node in as_array(workflow.get("nodes"))))
    node_set = set(node_ids)
    incoming = dict.fromkeys(node_ids, 0)
    outgoing = dict.fromkeys(node_ids, 0)
    adjacency = {node_id: [] for node_id in node_ids}
    for edge in as_array(workflow.get("edges")):
        source, target = edge.get("from"), edge.get("to")
        if source not in node_set or target not in node_set:
            continue
        outgoing[source] += 1
        incoming[target] += 1
        if target not in adjacency[source]:
            adjacency[source].append(target)

    diagnostics = []

    def add(code, message, subject, evidence, supported_fixes):
        diagnostics.append({
            "code": code,
            "severity": "error",
            "message": message,
            "subject": {"diagramType": "workflow", **subject},
            "evidence": evidence,
            "supportedFixes": supported_fixes,
        })

    references = []
    references.extend({"id": node_id, "path": f"/semanticChecks/allowedRoots/{index}"}
                      for index, node_id in enumerate(as_array(checks.get("allowedRoots"))))
    references.extend({"id": node_id, "path": f"/semanticChecks/allowedTerminals/{index}"}
                      for index, node_id in enumerate(as_array(checks.get("allowedTerminals"))))
    for key in ("requiredEdges", "requiredPaths"):
        for index, relation in enumerate(as_array(checks.get(key))):
            references.extend((
                {"id": relation.get("from"), "path": f"/semanticChecks/{key}/{index}/from"},
                {"id": relation.get("to"), "path": f"/semanticChecks/{key}/{index}/to"},
            ))
    for reference in references:
        node_id, path = reference["id"], reference["path"]
        if node_id in node_set:
            continue
        add(
            "workflow/semantic-node-reference",
            f'Workflow semantic contract references unknown node "{node_id}" at {path}.',
            {"node": node_id, "path": path},
            {"knownNodes": node_ids},
            [f'replace "{node_id}" with an existing node id', "add the missing node before compiling"],
        )
    if diagnostics:
        return diagnostics

    if "allowedRoots" in checks:
        allowed = checks["allowedRoots"]
        for node_id, count in incoming.items():
            if count or node_id in allowed:
                continue
            add(
                "workflow/unexpected-root",
                f'Workflow node "{node_id}" has no incoming edge and is not declared in semanticChecks.allowedRoots.',
                {"node": node_id, "path": "/semanticChecks/allowedRoots"},
                {"incomingEdges": 0, "allowedRoots": list(dict.fromkeys(allowed))},
                [f'add the missing incoming edge to "{node_id}"',
                 f'declare "{node_id}" in semanticChecks.allowedRoots if it is an intentional source'],
            )

    if "allowedTerminals" in checks:
        allowed = checks["allowedTerminals"]
        for node_id, count in outgoing.items():
            if count or node_id in allowed:
                continue
            add(
                "workflow/unexpected-terminal",
                f'Workflow node "{node_id}" has no outgoing edge and is not declared in semanticChecks.allowedTerminals.',
                {"node": node_id, "path": "/semanticChecks/allowedTerminals"},
                {"outgoingEdges": 0, "allowedTerminals": list(dict.fromkeys(allowed))},
                [f'add the missing outgoing edge from "{node_id}"',
                 f'declare "{node_id}" in semanticChecks.allowedTerminals if it is an intentional sink'],
            )

    authored_edges = {(edge.get("from"), edge.get("to")) for edge in as_array(workflow.get("edges"))}
    for index, relation in enumerate(as_array(checks.get("requiredEdges"))):
        source, target = relation.get("from"), relation.get("to")
        if (source, target) in authored_edges:
            continue
        add(
            "workflow/required-edge",
            f'Workflow semantic contract requires edge "{source}" -> "{target}", but no authored edge matches it.',
            {"from": source, "to": target, "path": f"/semanticChecks/requiredEdges/{index}"},
            {"authoredEdgeCount": len(as_array(workflow.get("edges")))},
            [f'add an edge from "{source}" to "{target}" without deleting the semantic requirement'],
        )

    def reachable(source, target):
        visited, pending = {source}, [source]
        while pending:
            current = pending.pop(0)
            if current == target:
                return True
            for next_node in adjacency.get(current, []):
                if next_node not in visited:
                    visited.add(next_node)
                    pending.append(next_node)
        return False

    for index, relation in enumerate(as_array(checks.get("requiredPaths"))):
        source, target = relation.get("from"), relation.get("to")
        if reachable(source, target):
            continue
        reachable_nodes = [source, *adjacency.get(source, [])]
        add(
            "workflow/required-path",
            f'Workflow semantic contract requires a directed path from "{source}" to "{target}", but none exists.',
            {"from": source, "to": target, "path": f"/semanticChecks/requiredPaths/{index}"},
            {"reachableNodes": list(dict.fromkeys(reachable_nodes))},
            [f'restore a directed path from "{source}" to "{target}" without weakening the semantic requirement'],
        )
    return diagnostics
