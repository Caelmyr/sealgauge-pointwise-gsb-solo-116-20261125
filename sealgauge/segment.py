"""Plan and crop a shareable source segment made of complete original columns.

A column only counts when it contains one continuous dark impression that is
wide enough. Isolated dark dots cannot be joined, and clipped pixels (any RGB
channel at 0 or 255) are never allowed to pad a width, so a column that fails
blocks crossing it.
"""

from io import BytesIO
import math

import numpy as np
from PIL import Image

from image_lab.validation import Invalid, number

from .seam import LUMA


def request_columns(value, config):
    length_mm = number(value.get("length_mm"), "要留的长度", 0.001, 100000)
    width_mm = number(value.get("min_width_mm"), "最小暗压痕宽", 0.001, 100000)
    scale = config["mm_per_px"]
    columns = math.ceil(length_mm / scale - 1e-9)
    min_width = math.ceil(width_mm / scale - 1e-9)
    frame_columns = config["right"] - config["left"] + 1
    if columns > frame_columns:
        raise Invalid(f"要留的长度超过测量框（框内只有 {frame_columns} 列 / "
                      f"{frame_columns * scale:.2f} mm）")
    if min_width > config["bottom"] - config["top"] + 1:
        raise Invalid("最小暗压痕宽超过测量框上下范围")
    return {"length_mm": length_mm, "min_width_mm": width_mm,
            "columns": columns, "min_width": min_width}


def column_facts(region, cutoff):
    """Return per-column impression facts inside the frame region."""
    luma = region @ LUMA
    dark = (luma <= cutoff) & np.all(region > 0, axis=-1) & np.all(region < 255, axis=-1)
    clipped = np.any((region == 0) | (region == 255), axis=-1)
    # Vectorized consecutive-run length per column, reset to 0 on a non-dark row.
    run = np.zeros(dark.shape, dtype=np.int32)
    run[0] = dark[0].astype(np.int32)
    for y in range(1, dark.shape[0]):
        run[y] = np.where(dark[y], run[y - 1] + 1, 0)
    longest = run.max(axis=0)
    end = run.argmax(axis=0)
    dark_counts = dark.sum(axis=0)
    clipped_counts = clipped.sum(axis=0)
    facts = []
    for offset in range(region.shape[1]):
        best = int(longest[offset])
        if best:
            y_end = int(end[offset])
            run_y = [y_end - best + 1, y_end]
        else:
            run_y = None
        facts.append({"longest_px": best, "run_y": run_y,
                      "dark_px": int(dark_counts[offset]),
                      "clipped_px": int(clipped_counts[offset])})
    return facts


def split_runs(passing):
    runs = []
    start = None
    for index, passed in enumerate(passing):
        if passed and start is None:
            start = index
        if not passed and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(passing) - 1))
    return runs


def plan(image, config, request):
    x0, x1, y0, y1 = (config[k] for k in ("left", "right", "top", "bottom"))
    wanted = request_columns(request, config)
    needed, min_width, scale = wanted["columns"], wanted["min_width"], config["mm_per_px"]
    region = image[y0:y1 + 1, x0:x1 + 1]
    facts = column_facts(region, config["cutoff"])
    columns, blocked = [], []
    qualified = split_runs([fact["longest_px"] >= min_width for fact in facts])
    for offset, fact in enumerate(facts):
        x = x0 + offset
        if fact["longest_px"] >= min_width:
            reason = None
            passed = True
        else:
            if fact["clipped_px"]:
                reason = "含 0/255 削幅像素，不能凑宽"
            elif fact["dark_px"] == 0:
                reason = "框内无有效暗像素"
            else:
                reason = "最长连续暗痕不足，暗点不能相连"
            passed = False
            blocked.append({"x": x, "reason": reason,
                            "longest_px": fact["longest_px"], "clipped_px": fact["clipped_px"]})
        columns.append({"x": x, "passed": passed, "reason": reason,
                        "longest_px": fact["longest_px"], "dark_px": fact["dark_px"],
                        "clipped_px": fact["clipped_px"],
                        "run_y": None if fact["run_y"] is None
                        else [y0 + fact["run_y"][0], y0 + fact["run_y"][1]]})

    candidates = []
    for run_offset, (lo, hi) in enumerate(qualified):
        if hi - lo + 1 < needed:
            continue
        windows = []
        for start in range(lo, hi - needed + 2):
            end = start + needed - 1
            narrow = min(facts[index]["longest_px"] for index in range(start, end + 1))
            windows.append((start, end, narrow))
        focus_offset = config["focus"] - x0
        # Inside one qualified run keep the window nearest the current cut line,
        # then prefer the larger narrowest-width margin, then the left edge.
        best_window = min(windows, key=lambda item: (
            min(abs(item[0] - focus_offset), abs(item[1] - focus_offset)),
            -(item[2] - min_width), item[0]))
        start, end, narrow = best_window
        candidates.append({
            "run": run_offset,
            "left_x": x0 + start, "right_x": x0 + end,
            "columns": needed,
            "length_mm": round(needed * scale, 6),
            "narrowest_px": narrow,
            "narrowest_mm": round(narrow * scale, 6),
            "margin_px": narrow - min_width,
            "margin_mm": round((narrow - min_width) * scale, 6),
            "distance_px": min(abs(x0 + start - config["focus"]), abs(x0 + end - config["focus"])),
            "contains_focus": x0 + start <= config["focus"] <= x0 + end,
        })
    # First stay near the current cut line; among equally near choices prefer
    # more headroom at the narrowest point, then the smaller left edge.
    candidates.sort(key=lambda item: (item["distance_px"], -item["margin_px"], item["left_x"]))
    for rank, item in enumerate(candidates):
        item["recommended"] = rank == 0
    return {"request": wanted, "frame": {"left": x0, "right": x1, "top": y0, "bottom": y1},
            "focus": config["focus"], "cutoff": config["cutoff"], "mm_per_px": scale,
            "columns": columns, "blocked": blocked,
            "qualified_runs": [{"left_x": x0 + lo, "right_x": x0 + hi, "columns": hi - lo + 1}
                               for lo, hi in qualified],
            "candidates": candidates,
            "selected": candidates[0]["left_x"] if candidates else None}


def crop(image, config, left_x, right_x):
    x0, x1, y0, y1 = (config[k] for k in ("left", "right", "top", "bottom"))
    if not (x0 <= left_x <= right_x <= x1):
        raise Invalid("留段左右边必须位于测量框内且左不越右")
    segment = image[y0:y1 + 1, left_x:right_x + 1]
    origin = {"source_width": int(image.shape[1]), "source_height": int(image.shape[0]),
              "left_x": int(left_x), "right_x": int(right_x), "top_y": int(y0), "bottom_y": int(y1)}
    return segment, origin


def preview_png(image, config, left_x, right_x):
    segment, _ = crop(image, config, left_x, right_x)
    stream = BytesIO()
    Image.fromarray(segment).save(stream, format="PNG")
    return stream.getvalue()
