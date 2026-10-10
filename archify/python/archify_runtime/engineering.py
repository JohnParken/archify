"""Deployment ownership validation using the existing engineering contract."""

import json

from .common import DiagnosticError, diagnostic

PROFILE = "deployment-ownership"
BOUNDARY_KINDS = ("region", "security-group")


def validate_engineering_profile(diagram_type, diagram):
    if diagram_type != "architecture" or diagram["meta"].get("engineering_profile") != PROFILE:
        return
    components = diagram.get("components", [])
    boundaries = diagram.get("boundaries", [])
    connections = diagram.get("connections", [])
    entries = []

    def quoted(value):
        return json.dumps(value, ensure_ascii=False)

    def subject(collection, index, item=None):
        result = {"diagramType": "architecture", "profile": PROFILE, "collection": collection, "index": index}
        if item and item.get("id"):
            result["id"] = item["id"]
        return result

    def memberships(component_id, kind):
        return [(i, b) for i, b in enumerate(boundaries) if b.get("kind") == kind and component_id in b.get("wraps", [])]

    def add(code, message, sub, evidence, fixes):
        entries.append(diagnostic("engineering/" + code, message, sub, evidence, fixes))

    for kind in BOUNDARY_KINDS:
        count = sum(b.get("kind") == kind for b in boundaries)
        if count == 0:
            add("deployment-boundary-kind", f"Deployment ownership requires at least one {kind} boundary.",
                subject("boundaries", -1), {"requiredKind": kind, "found": count},
                [f"add one {kind} boundary with an explicit wraps list"])
    for index, component in enumerate(components):
        if component.get("type") == "external":
            continue
        sub = subject("components", index, component)
        identity = quoted(component["id"])
        if not isinstance(component.get("tag"), str) or not component["tag"].strip():
            add("deployment-owner-missing", f"Deployment component {identity} does not name its owner in tag.",
                sub, {"componentType": component["type"], "ownerField": "tag"},
                [f"set /components/{index}/tag to the responsible team or owner"])
        regions = memberships(component["id"], "region")
        if not regions:
            add("deployment-region-scope", f"Deployment component {identity} is not assigned to a region boundary.",
                sub, {"componentType": component["type"], "regionMemberships": 0},
                ["add the component id to the real region boundary wraps list"])
        elif len(regions) > 1:
            add("deployment-region-ambiguous", f"Deployment component {identity} belongs to more than one region boundary.",
                sub, {"componentType": component["type"], "regions": [{"boundaryIndex": i, "label": b["label"]} for i, b in regions]},
                ["keep the component id in exactly one real region boundary wraps list"])
        if component["type"] == "database" and not memberships(component["id"], "security-group"):
            add("deployment-private-state", f"Stateful component {identity} is not assigned to a private security-group boundary.",
                sub, {"componentType": component["type"], "privateMemberships": 0},
                ["add the component id to the real private security-group boundary wraps list"])
    for index, boundary in enumerate(boundaries):
        if boundary.get("kind") != "security-group":
            continue
        members = [{"id": cid, "regions": [{"boundaryIndex": i, "label": b["label"]} for i, b in memberships(cid, "region")]}
                   for cid in boundary.get("wraps", [])]
        region_indexes = {r["boundaryIndex"] for m in members for r in m["regions"]}
        if members and all(len(m["regions"]) == 1 for m in members) and len(region_indexes) == 1:
            continue
        add("deployment-private-region-consistency", f'Private boundary {quoted(boundary["label"])} must contain components from exactly one shared region.',
            subject("boundaries", index, boundary), {"boundaryKind": boundary["kind"], "members": members},
            ["assign every private-boundary component to exactly one shared region boundary"])
    for index, connection in enumerate(connections):
        crossed = [{"boundaryIndex": i, "kind": b["kind"], "label": b["label"]} for i, b in enumerate(boundaries)
                   if b.get("kind") in BOUNDARY_KINDS and (connection["from"] in b.get("wraps", [])) != (connection["to"] in b.get("wraps", []))]
        if not crossed or (isinstance(connection.get("label"), str) and connection["label"].strip()):
            continue
        identity = connection.get("id") or connection["from"] + "->" + connection["to"]
        add("deployment-crossing-mechanism", f"Cross-boundary connection {quoted(identity)} does not name its mechanism.",
            subject("connections", index, connection), {"from": connection["from"], "to": connection["to"], "crossedBoundaries": crossed},
            [f"set /connections/{index}/label to the real cross-boundary mechanism"])
    if entries:
        raise DiagnosticError('Engineering profile "deployment-ownership" failed:\n' + "\n".join("- " + e["message"] for e in entries), entries)
