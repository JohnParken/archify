"""The canonical Viewer desktop budget, expressed without a JS runtime."""

from .geometry import is_finite_point


def declared_wide_readability_budget(view_box_width, view_box_height, minimum_source_text_px,
                                     requested_minimum_text_px, viewport_width=1440, body_horizontal_px=64,
                                     diagram_horizontal_px=30, minimum_reader_width=960, maximum_reader_width=1920):
    values = (view_box_width, view_box_height, minimum_source_text_px, requested_minimum_text_px,
              viewport_width, body_horizontal_px, diagram_horizontal_px, minimum_reader_width, maximum_reader_width)
    if (not is_finite_point(*values) or min(view_box_width, view_box_height, minimum_source_text_px,
                                          requested_minimum_text_px, viewport_width, minimum_reader_width) <= 0
            or body_horizontal_px < 0 or diagram_horizontal_px < 0 or maximum_reader_width < minimum_reader_width
            or view_box_width / view_box_height < 1.55):
        return None
    target = max(6, requested_minimum_text_px)
    scale = min(1, target / minimum_source_text_px)
    desired = max(minimum_reader_width, view_box_width * scale + diagram_horizontal_px)
    viewport_cap = max(0, viewport_width - body_horizontal_px)
    actual = min(desired, min(maximum_reader_width, viewport_cap))
    guaranteed = max(0, actual - diagram_horizontal_px)
    projected = minimum_source_text_px * min(1, guaranteed / view_box_width)
    limit = ("viewport-cap" if viewport_cap <= maximum_reader_width else "reader-cap") if actual < desired else "source-size"
    return {"requestedTargetPx": target, "requestedScale": scale, "desiredReaderWidth": desired,
            "viewportCap": viewport_cap, "maximumReaderWidth": maximum_reader_width, "actualReaderWidth": actual,
            "guaranteedSvgWidth": guaranteed, "projectedMinimumTextPx": projected, "hardFloorPx": 6,
            "hardFloorMet": projected >= 6, "requestedTargetMet": projected >= requested_minimum_text_px, "limit": limit}
