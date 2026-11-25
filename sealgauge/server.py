"""Connect the pressed seam desk to the photo preparation workspace."""

from io import BytesIO
import os
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file, url_for

from image_lab import create_app as preparation_app
from image_lab.validation import Invalid, integer, object_value

from . import exports, seam, segment
from .store import Cards


def download(data, mime, filename):
    return send_file(BytesIO(data), mimetype=mime, max_age=0, download_name=filename, as_attachment=True)


def create_app(directory=None):
    root = Path(directory or os.environ.get("SEALGAUGE_DATA", ".sealgauge-data")).resolve()
    desk = preparation_app(root / "prepare")
    cards = Cards(root / "cards")
    api = Blueprint("sealgauge", __name__, url_prefix="/sealgauge")

    @desk.get("/")
    def home():
        return render_template("sealgauge.html")

    @api.get("/health")
    def health():
        return jsonify(status="ok", app="sealgauge")

    @api.get("/cards")
    def listing():
        return jsonify(cards=cards.list())

    @api.post("/cards")
    def create():
        request.max_content_length = 20 * 1024 * 1024
        upload = request.files.get("photo")
        if not upload or not upload.filename:
            raise Invalid("请选择封边原照")
        size, image = seam.decode(upload.stream.read(seam.MAX_IMAGE_BYTES + 1))
        return jsonify(cards.create(request.form.get("name", ""), Path(upload.filename).name[:140],
                                    size, image, seam.defaults(size))), 201

    @api.post("/practice")
    def practice():
        return jsonify(cards.demo()), 201

    @api.route("/cards/<key>", methods=["GET", "PATCH"])
    def card(key):
        return jsonify(cards.update(key, object_value(request.get_json(silent=True), "封边卡设置"))
                       if request.method == "PATCH" else cards.read(key))

    @api.get("/cards/<key>/capture.png")
    def capture(key):
        return send_file(cards.photo(key), mimetype="image/png", conditional=True)

    def measurement(key):
        card = cards.read(key, request.args.get("revision"))
        image = seam.pixels(cards.photo(key))
        return card, image, seam.report(image, card["settings"])

    @api.get("/cards/<key>/measurement")
    def measurement_json(key):
        card, _, result = measurement(key)
        return jsonify(revision=card["revision"], **result)

    @api.get("/cards/<key>/probe")
    def probe(key):
        card = cards.read(key, request.args.get("revision"))
        x = integer(request.args.get("x"), "X", 0, card["width"] - 1)
        y = integer(request.args.get("y"), "Y", 0, card["height"] - 1)
        return jsonify(seam.point(seam.pixels(cards.photo(key)), x, y, card["settings"]))

    @api.get("/cards/<key>/export/profile.csv")
    def profile(key):
        _, _, result = measurement(key)
        return download(exports.trace_table(result), "text/csv", "seam-profile.csv")

    @api.get("/cards/<key>/export/proof.png")
    def proof(key):
        card, image, result = measurement(key)
        return download(exports.proof(image, card, result), "image/png", "seam-proof.png")

    @api.get("/cards/<key>/export/observation.zip")
    def bundle(key):
        card, image, result = measurement(key)
        package = exports.package(card, cards.photo(key).read_bytes(), image, result)
        return download(package, "application/zip", f"sealgauge-r{card['revision']}.zip")

    @api.get("/cards/<key>/segment/plan")
    def segment_plan(key):
        card = cards.read(key, request.args.get("revision"))
        image = seam.pixels(cards.photo(key))
        result = segment.plan(image, card["settings"], request.args)
        return jsonify(revision=card["revision"], **result)

    @api.get("/cards/<key>/segment/preview.png")
    def segment_preview(key):
        card = cards.read(key, request.args.get("revision"))
        config = card["settings"]
        left_x = integer(request.args.get("left_x"), "留段左 X", config["left"], config["right"])
        right_x = integer(request.args.get("right_x"), "留段右 X", left_x, config["right"])
        image = seam.pixels(cards.photo(key))
        return download(segment.preview_png(image, config, left_x, right_x),
                        "image/png", "seam-segment-preview.png")

    @api.post("/cards/<key>/prepare")
    def prepare(key):
        card = cards.read(key, request.args.get("revision"))
        image = seam.pixels(cards.photo(key)).astype("float32") / 255
        services = desk.extensions
        asset = services["assets"].put(image, card["photo_name"])
        project = services["store"].create(f'{card["name"]} · 原片准备')
        services["frames"].add(project["id"], project["revision"], [asset["id"]])
        return jsonify(url=url_for("index", project=project["id"])), 201

    @api.post("/cards/<key>/segment/prepare")
    def segment_prepare(key):
        card = cards.read(key, request.args.get("revision"))
        body = object_value(request.get_json(silent=True), "留段准备参数")
        config = card["settings"]
        left_x = integer(body.get("left_x"), "留段左 X", config["left"], config["right"])
        right_x = integer(body.get("right_x"), "留段右 X", left_x, config["right"])
        image = seam.pixels(cards.photo(key))
        # Re-derive the plan from the same request so a segment can never cross
        # a column that failed the continuous dark-impression rule.
        result = segment.plan(image, config, body)
        if not any(item["left_x"] == left_x and item["right_x"] == right_x
                   for item in result["candidates"]):
            raise Invalid("该区间不是合格留段：含未过列或宽度不满足要求，请重新规划后采用")
        raw, origin = segment.crop(image, config, left_x, right_x)
        services = desk.extensions
        name = f'{card["name"]} · 留段 X{left_x}-{right_x}'
        asset = services["assets"].put(raw.astype("float32") / 255, name + ".png",
                                       {"sealgauge_source": card["photo_name"], **origin})
        project = services["store"].create(f'{card["name"]} · 留段准备')
        outcome = services["frames"].add(project["id"], project["revision"], [asset["id"]])
        added = outcome["project"]
        frame = next(item for item in added["frames"] if item["asset"] == asset["id"])
        services["frames"].update(project["id"], added["revision"], frame["id"], {
            "notes": (f"源自 {card['photo_name']} 原片像素：左X {left_x}，右X {right_x}，"
                      f"上Y {origin['top_y']}，下Y {origin['bottom_y']}；"
                      f"原片尺寸 {origin['source_width']}×{origin['source_height']} px")})
        return jsonify(url=url_for("index", project=project["id"]),
                       project_id=project["id"], origin=origin, frame_id=frame["id"]), 201

    desk.register_blueprint(api)
    return desk
