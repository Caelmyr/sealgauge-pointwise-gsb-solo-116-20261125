"""Exercise the segment planning rules and Flask wiring with synthetic images."""

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/pointwise-env/site-packages")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from sealgauge import seam, segment  # noqa: E402
from sealgauge.server import create_app  # noqa: E402


def make_image():
    rng = np.random.default_rng(7)
    image = rng.integers(200, 230, size=(60, 80, 3), dtype=np.uint8)
    config = seam.settings({"left": 10, "right": 69, "top": 20, "bottom": 49,
                            "focus": 40, "cutoff": 151, "mm_per_px": 0.5}, (80, 60))

    def dark_band(x0, x1, y0, y1, value=90):
        image[y0:y1 + 1, x0:x1 + 1] = value

    # Qualified runs (need width >= 8 px): columns 10-19 (width 10), 30-49 (width 12)
    dark_band(10, 19, 25, 34)
    dark_band(30, 49, 22, 33)
    # Column 20-29: isolated dark dots only (each column only 2 separated dark pixels)
    for x in range(20, 30):
        image[21, x] = 80
        image[40, x] = 80
    # Column 50: contains a 0-clipped pixel; its dark band cannot count for width
    dark_band(50, 50, 22, 33)
    image[27, 50] = 0
    # Column 51: 255-clipped bright pixel inside an otherwise dark band
    dark_band(51, 51, 22, 33)
    image[30, 51] = 255
    # Columns 52-69: clean qualified band, width 9
    dark_band(52, 69, 24, 32)
    return image, config


def test_core_rules():
    image, config = make_image()
    plan = segment.plan(image, config, {"length_mm": 30.0, "min_width_mm": 4.0})
    # 30 mm / 0.5 = 60 px -> 60 columns; min width 4 mm -> 8 px
    assert plan["request"]["columns"] == 60, plan["request"]
    assert plan["request"]["min_width"] == 8
    blocked = {row["x"]: row for row in plan["blocked"]}
    # Column 51 keeps two 11-px continuous pieces around one 255 pixel, so it
    # still passes the 8-px minimum; only column 50 (0 inside a 12-px band
    # split into 5+6) is blocked by clipping.
    assert set(blocked) == set(range(20, 30)) | {50}, sorted(blocked)
    assert "暗点" in blocked[20]["reason"]
    assert "削幅" in blocked[50]["reason"]
    assert blocked[20]["longest_px"] == 1
    col51 = next(row for row in plan["columns"] if row["x"] == 51)
    assert col51["passed"] and col51["longest_px"] == 8 and col51["clipped_px"] == 1
    runs = [(r["left_x"], r["right_x"]) for r in plan["qualified_runs"]]
    assert runs == [(10, 19), (30, 49), (51, 69)], runs
    # 60 adjacent qualified columns do not exist anywhere
    assert plan["candidates"] == [] and plan["selected"] is None

    # Smaller request: 20 mm -> 40 columns; only run 30-49 has 20 columns -> still none
    plan = segment.plan(image, config, {"length_mm": 20.0, "min_width_mm": 4.0})
    assert plan["request"]["columns"] == 40 and plan["candidates"] == []

    # 10 mm -> 20 columns: run 30-49 (20 cols) qualifies; focus 40 inside it
    plan = segment.plan(image, config, {"length_mm": 10.0, "min_width_mm": 4.0})
    assert plan["request"]["columns"] == 20
    assert len(plan["candidates"]) == 1
    pick = plan["candidates"][0]
    assert (pick["left_x"], pick["right_x"]) == (30, 49), pick
    assert pick["contains_focus"] and pick["distance_px"] == 9
    assert pick["narrowest_px"] == 12 and pick["margin_px"] == 4
    assert pick["length_mm"] == 10.0

    # 8 mm -> 16 columns: windows in runs (10-19 too short), 30-49 and 51-69 (19 cols)
    plan = segment.plan(image, config, {"length_mm": 8.0, "min_width_mm": 4.0})
    assert plan["request"]["columns"] == 16
    assert [(c["left_x"], c["right_x"]) for c in plan["candidates"]] == [(30, 45), (51, 66)]
    # nearest width-16 window in 30..49 starts at 30 and contains the cut line X40
    assert plan["candidates"][0]["distance_px"] == 5
    assert plan["candidates"][0]["contains_focus"]

    # Rounding: 8.1 mm / 0.5 = 16.2 -> 17 columns
    plan = segment.plan(image, config, {"length_mm": 8.1, "min_width_mm": 3.5})
    assert plan["request"]["columns"] == 17 and plan["request"]["min_width"] == 7

    # Tight width 4.5 mm -> 9 px: col 51 longest run is 8 so it drops, run 52-69 still passes
    plan = segment.plan(image, config, {"length_mm": 4.0, "min_width_mm": 4.5})
    pairs = {(c["left_x"], c["right_x"]) for c in plan["candidates"]}
    assert (52, 59) in pairs and (33, 40) in pairs
    # Focus 40: the window ending on the cut line (33-40) must be recommended first
    assert plan["candidates"][0]["left_x"] == 33

    # Width beyond frame rejected
    try:
        segment.plan(image, config, {"length_mm": 4.0, "min_width_mm": 99.0})
    except seam.Invalid:
        pass
    else:
        raise AssertionError("oversized width should be rejected")
    print("core rules OK")


def test_ranking_and_crop():
    image, config = make_image()
    # 4 mm -> 8 columns: all three runs offer windows; check near-focus ranking
    plan = segment.plan(image, config, {"length_mm": 4.0, "min_width_mm": 4.0})
    assert plan["candidates"][0]["recommended"] is True
    assert all(c["recommended"] is False for c in plan["candidates"][1:])
    first = plan["candidates"][0]
    assert 30 <= first["left_x"] <= 49 and first["contains_focus"], first

    raw, origin = segment.crop(image, config, first["left_x"], first["right_x"])
    assert raw.shape == (30, 8, 3), raw.shape
    assert origin == {"source_width": 80, "source_height": 60,
                      "left_x": first["left_x"], "right_x": first["right_x"],
                      "top_y": 20, "bottom_y": 49}
    assert np.array_equal(raw, image[20:50, first["left_x"]:first["right_x"] + 1])
    png_bytes = segment.preview_png(image, config, first["left_x"], first["right_x"])
    with Image.open(io.BytesIO(png_bytes)) as preview:
        assert preview.size == (8, 30)
    # Out-of-frame crop rejected
    for bad in ((9, 20), (20, 70), (30, 25)):
        try:
            segment.crop(image, config, *bad)
        except seam.Invalid:
            continue
        raise AssertionError(f"crop {bad} should be rejected")
    print("ranking and crop OK")


def test_flask():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(tmp)
        client = app.test_client()
        assert client.get("/sealgauge/health").status_code == 200
        resp = client.post("/sealgauge/practice")
        assert resp.status_code == 201, resp.get_json()
        card = resp.get_json()
        key, revision = card["id"], card["revision"]
        cfg = card["settings"]

        # Plan endpoint validation
        bad = client.get(f"/sealgauge/cards/{key}/segment/plan?revision={revision}"
                         "&length_mm=-1&min_width_mm=1")
        assert bad.status_code == 400

        # Practice image: choose a modest request so candidates exist
        ok = client.get(f"/sealgauge/cards/{key}/segment/plan?revision={revision}"
                        "&length_mm=10&min_width_mm=1")
        assert ok.status_code == 200, ok.get_json()
        plan = ok.get_json()
        assert plan["revision"] == revision
        if plan["candidates"]:
            pick = plan["candidates"][0]
            pv = client.get(f"/sealgauge/cards/{key}/segment/preview.png?revision={revision}"
                            f"&left_x={pick['left_x']}&right_x={pick['right_x']}")
            assert pv.status_code == 200 and pv.mimetype == "image/png"
            with Image.open(io.BytesIO(pv.data)) as shot:
                assert shot.size == (pick["columns"], cfg["bottom"] - cfg["top"] + 1)
            sent = client.post(f"/sealgauge/cards/{key}/segment/prepare?revision={revision}",
                               json={"left_x": pick["left_x"], "right_x": pick["right_x"],
                                     "length_mm": 10, "min_width_mm": 1})
            assert sent.status_code == 201, sent.get_json()
            payload = sent.get_json()
            assert payload["origin"]["left_x"] == pick["left_x"]
            project = client.get("/api/projects/" + payload["project_id"]).get_json()
            frame = project["frames"][0]
            assert frame["notes"].startswith("源自 ") and "左X" in frame["notes"]
            assert f"原片尺寸 {card['width']}×{card['height']} px" in frame["notes"]

            # A window spanning a blocked column must be rejected server-side
            cheat = client.post(f"/sealgauge/cards/{key}/segment/prepare?revision={revision}",
                                json={"left_x": cfg["left"], "right_x": cfg["left"] + pick["columns"] - 1,
                                      "length_mm": 10, "min_width_mm": 1})
            assert cheat.status_code == 400, cheat.get_json()
        else:
            print("practice image had no candidates at this request")

        # Existing whole-photo prepare flow still works
        whole = client.post(f"/sealgauge/cards/{key}/prepare?revision={revision}")
        assert whole.status_code == 201
        print("flask wiring OK")


if __name__ == "__main__":
    test_core_rules()
    test_ranking_and_crop()
    test_flask()
    print("ALL SEGMENT TESTS PASSED")
