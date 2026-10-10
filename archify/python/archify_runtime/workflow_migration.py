"""Geometry-preserving projections used while migrating Workflow v1 to v2."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import math

TARGET_SCHEMA_VERSION = 2
LEGACY_COLUMN_CENTERS = (88, 220, 300, 430, 500, 625)


def intrinsic_workflow(workflow):
    """Return a schema-v2 projection without an authored viewBox."""
    intrinsic = deepcopy(workflow)
    intrinsic["schema_version"] = TARGET_SCHEMA_VERSION
    intrinsic["meta"] = {**(intrinsic.get("meta") or {})}
    intrinsic["meta"].pop("viewBox", None)
    return intrinsic


def planning_workflow(workflow):
    """Project away route pins that may only work after rank remapping."""
    planned = intrinsic_workflow(workflow)
    projected = []
    for edge in planned.get("edges", []):
        has_routed_geometry = (
            isinstance(edge.get("via"), list)
            or (edge.get("route") and edge.get("route") not in ("auto", "straight"))
            or "channelX" in edge
            or "channelY" in edge
        )
        if has_routed_geometry:
            continue
        automatic = {key: edge[key] for key in ("id", "from", "to", "variant", "role", "width") if key in edge}
        if edge.get("route") == "straight":
            automatic["route"] = "straight"
        if "labelAt" not in edge and "label" in edge:
            automatic["label"] = edge["label"]
        projected.append(automatic)
    planned["edges"] = projected

    main_path = planned.get("mainPath")
    if isinstance(main_path, list):
        pairs = {(edge.get("from"), edge.get("to")) for edge in planned["edges"]}
        if any((main_path[index], main_path[index + 1]) not in pairs for index in range(len(main_path) - 1)):
            planned.pop("mainPath", None)
    return planned


def _mapped_number(value):
    # Match Number(value.toFixed(6)) without using a JavaScript runtime.
    rounded = Decimal.from_float(float(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return float(rounded)


def create_horizontal_rank_mapper(old_columns, new_columns):
    if (not isinstance(old_columns, (list, tuple)) or not isinstance(new_columns, (list, tuple))
            or len(old_columns) != len(new_columns) or len(old_columns) < 2
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                       for value in (*old_columns, *new_columns))):
        raise TypeError("Horizontal rank mapping requires matching finite column arrays.")
    if any(old_columns[index] <= old_columns[index - 1] or new_columns[index] <= new_columns[index - 1]
           for index in range(1, len(old_columns))):
        raise TypeError("Horizontal rank mapping requires strictly increasing columns.")

    def map_x(x):
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x):
            raise TypeError("Horizontal rank mapping requires a finite x coordinate.")
        segment = len(old_columns) - 2
        if x <= old_columns[0]:
            segment = 0
        else:
            for index in range(len(old_columns) - 1):
                if x <= old_columns[index + 1]:
                    segment = index
                    break
        old_span = old_columns[segment + 1] - old_columns[segment]
        new_span = new_columns[segment + 1] - new_columns[segment]
        ratio = (x - old_columns[segment]) / old_span
        return _mapped_number(new_columns[segment] + ratio * new_span)

    return map_x


def map_explicit_coordinates(workflow, map_x):
    """Map every absolute X pin and return changes in authored order."""
    changed = []

    def record(path, owner, property_name):
        before = owner[property_name]
        after = map_x(before)
        owner[property_name] = after
        if after != before:
            changed.append({"path": path, "from": before, "to": after})

    for edge_index, edge in enumerate(workflow.get("edges", [])):
        via = edge.get("via")
        if isinstance(via, list):
            for point_index, point in enumerate(via):
                if (isinstance(point, list) and len(point) and isinstance(point[0], (int, float))
                        and not isinstance(point[0], bool) and math.isfinite(point[0])):
                    record(f"/edges/{edge_index}/via/{point_index}/0", point, 0)
        label_at = edge.get("labelAt")
        if (isinstance(label_at, list) and len(label_at) and isinstance(label_at[0], (int, float))
                and not isinstance(label_at[0], bool) and math.isfinite(label_at[0])):
            record(f"/edges/{edge_index}/labelAt/0", label_at, 0)
        channel_x = edge.get("channelX")
        if isinstance(channel_x, (int, float)) and not isinstance(channel_x, bool) and math.isfinite(channel_x):
            record(f"/edges/{edge_index}/channelX", edge, "channelX")
    return changed


def create_mapped_workflow_candidate(workflow, old_columns, new_columns):
    document = deepcopy(workflow)
    document["schema_version"] = TARGET_SCHEMA_VERSION
    changed_coordinates = map_explicit_coordinates(
        document, create_horizontal_rank_mapper(old_columns, new_columns))
    return {"document": document, "changedCoordinates": changed_coordinates}
