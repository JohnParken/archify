"""Deterministic row/column orthogonal router for Lifecycle schema v2."""

from itertools import permutations

from .flow_geometry import rounded_path

PORT_GUTTER = 16
PORT_SPACING = 30
SNAP_LIMIT = 16
TRACK_TOP_CLEARANCE = 18
TRACK_BOTTOM_CLEARANCE = 18
PREFERRED_TRACK_SPACING = 22
MAX_TRACK_SPACING = 24
CORRIDOR_SPACING = 10
EXHAUSTIVE_TRACK_LIMIT = 7
OPPOSITE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}


class LifecycleGridRouter:
    """Plan lifecycle transitions once, then expose stable points and sides."""

    def __init__(self, states, transitions, *, row_of, column_xs):
        self.states = states
        self.transitions = transitions
        self._row_of = row_of
        self.plans = {}
        self.ports = {}
        self.track_y = {}
        self.track_counts = {}
        self.path_cache = {}
        self._transition_index = {id(item): index for index, item in enumerate(transitions)}
        by_row = {}
        for state in states.values():
            row = row_of(state)
            if _integer(row):
                by_row.setdefault(row, []).append(state)
        self.by_row = by_row
        self.rows = sorted(by_row)
        self.row_top = {row: min(s["y"] for s in by_row[row]) for row in self.rows}
        self.row_bottom = {row: max(s["y"] + s["height"] for s in by_row[row]) for row in self.rows}
        corridor_xs = [(x + column_xs[index]) / 2 for index, x in enumerate(column_xs[1:])]
        all_states = list(states.values())
        self.leftmost_x = min(state["cx"] for state in all_states)
        self.rightmost_x = max(state["cx"] for state in all_states)
        self.grid_left = min(state["x"] for state in all_states)
        self.grid_right = max(state["x"] + state["width"] for state in all_states)

        def next_row(row):
            return next((candidate for candidate in self.rows if candidate > row), None)

        def previous_row(row):
            return next((candidate for candidate in reversed(self.rows) if candidate < row), None)

        def gap_band(row):
            below = next_row(row)
            top = self.row_bottom[row] + TRACK_TOP_CLEARANCE
            bottom = self.row_bottom[row] + 40 if below is None else self.row_top[below] - TRACK_BOTTOM_CLEARANCE
            return top, max(top, bottom)

        def blocks_vertical(x, from_row, to_row, exclude):
            low, high = sorted((from_row, to_row))
            return any(state["id"] not in exclude and row_of(state) > low and row_of(state) < high
                       and x >= state["x"] - 6 and x <= state["x"] + state["width"] + 6
                       for state in all_states)

        def blocks_horizontal(source, target):
            row = row_of(source)
            left, right = sorted((source, target), key=lambda state: state["cx"])
            return any(state is not left and state is not right
                       and state["x"] < right["x"]
                       and state["x"] + state["width"] > left["x"] + left["width"]
                       and state["y"] < max(left["y"] + left["height"], right["y"] + right["height"])
                       and state["y"] + state["height"] > min(left["y"], right["y"])
                       for state in by_row[row])

        def loop_blocked(source, target, side):
            if side != "left":
                return False
            low, high = sorted((row_of(source), row_of(target)))
            return any(state["type"] == "start" and abs(state["cx"] - source["cx"]) < 1
                       and low <= row_of(state) <= high for state in all_states)

        for transition in transitions:
            source, target = states.get(transition.get("from")), states.get(transition.get("to"))
            if not source or not target or source is target:
                continue
            source_row, target_row = row_of(source), row_of(target)
            if not _integer(source_row) or not _integer(target_row):
                continue
            exclude = {source["id"], target["id"]}
            if source_row == target_row:
                if not blocks_horizontal(source, target):
                    side = "right" if target["cx"] > source["cx"] else "left"
                    self.plans[id(transition)] = {"kind": "horizontal", "from": source, "to": target,
                                                  "fromSide": side, "toSide": OPPOSITE[side]}
                else:
                    gap_row = previous_row(source_row)
                    use_above = gap_row is not None
                    side = "top" if use_above else "bottom"
                    self.plans[id(transition)] = {
                        "kind": "channel", "from": source, "to": target,
                        "fromSide": side, "toSide": side,
                        "runs": [{"gap": gap_row if use_above else source_row,
                                  "legs": ["down", "down"] if use_above else ["up", "up"]}],
                    }
                continue
            down = target_row > source_row
            from_side, to_side = (("bottom", "top") if down else ("top", "bottom"))
            gap_near_target = previous_row(target_row) if down else target_row
            gap_near_source = source_row if down else previous_row(source_row)
            legs = ["up", "down"] if down else ["down", "up"]
            if not blocks_vertical(source["cx"], source_row, target_row, exclude) and abs(source["cx"] - target["cx"]) < 1:
                plan = {"kind": "vertical", "from": source, "to": target,
                        "fromSide": from_side, "toSide": to_side,
                        "runs": [{"gap": gap_near_target, "legs": legs}]}
            elif not blocks_vertical(source["cx"], source_row, target_row, exclude):
                plan = {"kind": "channel", "from": source, "to": target,
                        "fromSide": from_side, "toSide": to_side,
                        "runs": [{"gap": gap_near_target, "legs": legs}]}
            elif not blocks_vertical(target["cx"], source_row, target_row, exclude):
                plan = {"kind": "channel", "from": source, "to": target,
                        "fromSide": from_side, "toSide": to_side,
                        "runs": [{"gap": gap_near_source, "legs": legs}]}
            elif (not transition.get("label") and not transition.get("note")
                  and abs(source["cx"] - target["cx"]) < 1
                  and (source["cx"] <= self.leftmost_x or source["cx"] >= self.rightmost_x)
                  and not loop_blocked(source, target, "left" if source["cx"] <= self.leftmost_x else "right")):
                side = "left" if source["cx"] <= self.leftmost_x else "right"
                plan = {"kind": "loop", "from": source, "to": target,
                        "fromSide": side, "toSide": side, "side": side}
            else:
                middle = (source["cx"] + target["cx"]) / 2
                corridor = next(iter(sorted(
                    (x for x in corridor_xs if not blocks_vertical(x, source_row, target_row, set())),
                    key=lambda x: abs(x - middle))), None)
                if corridor is None:
                    plan = {"kind": "channel", "from": source, "to": target,
                            "fromSide": from_side, "toSide": to_side,
                            "runs": [{"gap": gap_near_target, "legs": legs}]}
                else:
                    plan = {"kind": "corridor", "from": source, "to": target,
                            "fromSide": from_side, "toSide": to_side,
                            "corridor": corridor,
                            "runs": [{"gap": gap_near_source, "legs": legs},
                                     {"gap": gap_near_target, "legs": legs}]}
            self.plans[id(transition)] = plan

        corridor_use = {}
        for plan in self.plans.values():
            if plan["kind"] == "corridor":
                corridor_use.setdefault(plan["corridor"], []).append(plan)
        for x, plans in corridor_use.items():
            plans.sort(key=lambda item: (item["from"]["cx"], item["to"]["cx"]))
            for index, plan in enumerate(plans):
                plan["corridorX"] = x + (index - (len(plans) - 1) / 2) * CORRIDOR_SPACING
        for side in ("left", "right"):
            loops = sorted((plan for plan in self.plans.values()
                            if plan["kind"] == "loop" and plan["side"] == side),
                           key=lambda plan: abs(row_of(plan["from"]) - row_of(plan["to"])))
            for index, plan in enumerate(loops):
                plan["loopX"] = (self.grid_left - 18 - index * CORRIDOR_SPACING if side == "left"
                                  else self.grid_right + 18 + index * CORRIDOR_SPACING)

        def heading_for(plan, end):
            self_state = plan["from"] if end == "source" else plan["to"]
            other = plan["to"] if end == "source" else plan["from"]
            if plan["kind"] in ("horizontal", "loop"):
                return other["cy"]
            if plan["kind"] == "corridor":
                return plan["corridorX"]
            return self_state["cx"] if other is self_state else other["cx"]

        side_ends = {}
        for transition in transitions:
            plan = self.plans.get(id(transition))
            if not plan:
                continue
            for end in ("source", "target"):
                state = plan["from"] if end == "source" else plan["to"]
                side = plan["fromSide"] if end == "source" else plan["toSide"]
                key = (state["id"], side)
                positive = (plan["to"]["cx"] > plan["from"]["cx"] if plan["kind"] == "horizontal"
                            else row_of(plan["to"]) > row_of(plan["from"])
                            or (row_of(plan["to"]) == row_of(plan["from"])
                                and plan["to"]["cx"] > plan["from"]["cx"]))
                side_ends.setdefault(key, {"state": state, "side": side, "ends": []})["ends"].append(
                    {"transition": transition, "end": end,
                     "heading": heading_for(plan, end), "positive": positive})
        self.side_ends = side_ends
        for record in side_ends.values():
            state, side, ends = record["state"], record["side"], record["ends"]
            ends.sort(key=lambda item: (item["heading"], not item["positive"]))
            horizontal_side = side in ("top", "bottom")
            length = state["width"] if horizontal_side else state["height"]
            gutter = min(PORT_GUTTER, length / 4)
            spacing = min(PORT_SPACING, (length - gutter * 2) / (len(ends) - 1)) if len(ends) > 1 else 0
            for index, entry in enumerate(ends):
                offset = (index - (len(ends) - 1) / 2) * spacing
                point = ([state["cx"] + offset, state["y"] if side == "top" else state["y"] + state["height"]]
                         if horizontal_side else
                         [state["x"] if side == "left" else state["x"] + state["width"], state["cy"] + offset])
                self.ports.setdefault(id(entry["transition"]), {})[entry["end"]] = point

        def side_range(state, side):
            return ((state["x"] + PORT_GUTTER / 2, state["x"] + state["width"] - PORT_GUTTER / 2)
                    if side in ("top", "bottom") else
                    (state["y"] + PORT_GUTTER / 2, state["y"] + state["height"] - PORT_GUTTER / 2))

        def ports_on_side(state, side, except_transition):
            record = side_ends.get((state["id"], side), {})
            return [self.ports[id(item["transition"])][item["end"]]
                    for item in record.get("ends", []) if item["transition"] is not except_transition]

        for transition in transitions:
            plan = self.plans.get(id(transition))
            if not plan:
                continue
            record = self.ports[id(transition)]
            axis = 1 if plan["kind"] == "horizontal" else 0
            delta = abs(record["source"][axis] - record["target"][axis])
            if not (delta < 0.5 or delta >= SNAP_LIMIT or plan["kind"] not in ("horizontal", "vertical", "channel")):
                for end, fixed in (("target", "source"), ("source", "target")):
                    state = plan["from"] if end == "source" else plan["to"]
                    side = plan["fromSide"] if end == "source" else plan["toSide"]
                    value = record[fixed][axis]
                    low, high = side_range(state, side)
                    crowded = any(abs(point[axis] - value) < 10 for point in ports_on_side(state, side, transition))
                    if low <= value <= high and not crowded:
                        record[end] = ([record[end][0], value] if axis else [value, record[end][1]])
                        break

        runs_by_gap = {}
        for transition in transitions:
            plan = self.plans.get(id(transition))
            if not plan or plan["kind"] in ("horizontal", "loop"):
                continue
            source, target = self.ports[id(transition)]["source"], self.ports[id(transition)]["target"]
            for index, run in enumerate(plan["runs"]):
                x1 = source[0] if index == 0 else plan["corridorX"]
                x2 = plan["corridorX"] if plan["kind"] == "corridor" and index == 0 else target[0]
                if plan["kind"] != "corridor" and abs(x1 - x2) < 0.5:
                    continue
                runs_by_gap.setdefault(run["gap"], []).append(
                    {"transition": transition, "index": index, "x1": x1, "x2": x2,
                     "legs": run["legs"]})

        for gap, runs in runs_by_gap.items():
            top, bottom = gap_band(gap)
            sorted_runs = sorted(runs, key=lambda item: min(item["x1"], item["x2"]))
            classes = []
            for run in sorted_runs:
                low, high = min(run["x1"], run["x2"]) - 8, max(run["x1"], run["x2"]) + 8
                target_class = next((members for members in classes if all(
                    high < min(other["x1"], other["x2"]) - 8
                    or low > max(other["x1"], other["x2"]) + 8 for other in members)), None)
                if target_class is None or len(runs) <= EXHAUSTIVE_TRACK_LIMIT:
                    target_class = []
                    classes.append(target_class)
                target_class.append(run)
            count = len(classes)
            self.track_counts[gap] = count
            spacing = min(MAX_TRACK_SPACING, (bottom - top) / (count - 1)) if count > 1 else 0
            center = (top + bottom) / 2
            ys = [center + (index - (count - 1) / 2) * spacing for index in range(count)]

            def crossings(order):
                y_for = {id(run): ys[index] for index, members in enumerate(order) for run in members}
                total = 0
                for a in runs:
                    for b in runs:
                        if a is b or a["transition"] is b["transition"]:
                            continue
                        low, high = sorted((a["x1"], a["x2"]))
                        for x, leg in ((b["x1"], b["legs"][0]), (b["x2"], b["legs"][1])):
                            for ax, a_leg in ((a["x1"], a["legs"][0]), (a["x2"], a["legs"][1])):
                                if abs(ax - x) < 1 and a_leg == "up" and leg == "down" and y_for[id(b)] < y_for[id(a)]:
                                    total += 100
                            if x <= low + 0.5 or x >= high - 0.5:
                                continue
                            if (leg == "up" and y_for[id(a)] < y_for[id(b)]) or (leg == "down" and y_for[id(a)] > y_for[id(b)]):
                                total += 1
                return total

            best, best_score = classes, crossings(classes)
            if count <= EXHAUSTIVE_TRACK_LIMIT:
                for order in list(permutations(classes))[1:]:
                    score = crossings(order)
                    if score < best_score:
                        best, best_score = list(order), score
            else:
                improved = True
                while improved:
                    improved = False
                    for i in range(count):
                        if improved:
                            break
                        for j in range(i + 1, count):
                            order = list(best)
                            order[i], order[j] = order[j], order[i]
                            score = crossings(order)
                            if score < best_score:
                                best, best_score = order, score
                                improved = True
                                break
            for index, members in enumerate(best):
                for run in members:
                    key = (gap, run["index"], self._transition_index[id(run["transition"])])
                    self.track_y[key] = ys[index]

        def points_for(transition):
            plan = self.plans.get(id(transition))
            if not plan:
                state = states.get(transition.get("from")) or states.get(transition.get("to"))
                point = [state["x"] + state["width"], state["cy"]] if state else [0, 0]
                return [point, point]
            source, target = self.ports[id(transition)]["source"], self.ports[id(transition)]["target"]
            if plan["kind"] == "horizontal":
                if abs(source[1] - target[1]) < 0.5:
                    return [source, target]
                x = (source[0] + target[0]) / 2
                return [source, [x, source[1]], [x, target[1]], target]
            if plan["kind"] == "loop":
                return [source, [plan["loopX"], source[1]], [plan["loopX"], target[1]], target]
            def track_for(index):
                return self.track_y[(plan["runs"][index]["gap"], index, self._transition_index[id(transition)])]
            if plan["kind"] == "corridor":
                y1, y2 = track_for(0), track_for(1)
                return [source, [source[0], y1], [plan["corridorX"], y1],
                        [plan["corridorX"], y2], [target[0], y2], target]
            if abs(source[0] - target[0]) < 0.5:
                return [source, target]
            y = track_for(0)
            return [source, [source[0], y], [target[0], y], target]

        for transition in transitions:
            if id(transition) in self.plans:
                self.path_cache[id(transition)] = points_for(transition)

        def verticals(points):
            result = []
            for index, end in enumerate(points[1:]):
                start = points[index]
                if abs(start[0] - end[0]) < 0.5 and abs(start[1] - end[1]) >= 0.5:
                    result.append({"x": start[0], "low": min(start[1], end[1]),
                                   "high": max(start[1], end[1]), "index": index,
                                   "last": index == len(points) - 2})
            return result

        for _round in range(12):
            entries = [(transition, self.path_cache[id(transition)]) for transition in transitions
                       if id(transition) in self.path_cache]
            conflict = None
            for i, (left_transition, left_points) in enumerate(entries):
                for right_transition, right_points in entries[i + 1:]:
                    for left_segment in verticals(left_points):
                        right_segment = next((other for other in verticals(right_points)
                                              if abs(other["x"] - left_segment["x"]) < 1
                                              and min(other["high"], left_segment["high"])
                                              - max(other["low"], left_segment["low"]) > 0.5), None)
                        if right_segment:
                            conflict = ((left_transition, left_segment), (right_transition, right_segment))
                            break
                    if conflict:
                        break
                if conflict:
                    break
            if not conflict:
                break
            moved = False
            for transition, segment in conflict:
                plan = self.plans[id(transition)]
                end_name = "source" if segment["index"] == 0 else "target" if segment["last"] else None
                if not end_name or len(self.path_cache[id(transition)]) < 4 or plan["kind"] not in ("channel", "corridor"):
                    continue
                state = plan["from"] if end_name == "source" else plan["to"]
                side = plan["fromSide"] if end_name == "source" else plan["toSide"]
                record = self.ports[id(transition)]
                low, high = side_range(state, side)
                x = next((record[end_name][0] + delta for delta in (12, -12, 20, -20)
                          if low <= record[end_name][0] + delta <= high
                          and not any(abs(point[0] - (record[end_name][0] + delta)) < 10
                                      for point in ports_on_side(state, side, transition))), None)
                if x is None:
                    continue
                record[end_name] = [x, record[end_name][1]]
                self.path_cache[id(transition)] = points_for(transition)
                moved = True
                break
            if not moved:
                break

    def gap_height(self, row):
        count = self.track_counts.get(row, 0)
        return TRACK_TOP_CLEARANCE + TRACK_BOTTOM_CLEARANCE + max(0, count - 1) * PREFERRED_TRACK_SPACING

    def connection_sides(self, transition):
        plan = self.plans.get(id(transition))
        if plan:
            return {"fromSide": plan["fromSide"], "toSide": plan["toSide"]}
        return {"fromSide": "right", "toSide": "right"}

    def path_for(self, transition):
        points = self.path_cache.get(id(transition))
        if points is None:
            state = self.states.get(transition.get("from")) or self.states.get(transition.get("to"))
            point = [state["x"] + state["width"], state["cy"]] if state else [0, 0]
            points = [point, point]
        return points


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)
