"""Export the live source seam profile and current annotated proof."""

from io import BytesIO, StringIO
from zipfile import ZipFile, ZIP_DEFLATED
import csv
import json

from PIL import Image, ImageDraw

from .seam import png


def trace_table(result):
    buffer=StringIO()
    writer=csv.writer(buffer)
    writer.writerow(("x_px","y_px","luma","pressed"))
    for point in result["profile"]:
        writer.writerow((point["x"],point["y"],f'{point["brightness"]:.3f}',int(point["pressed"])))
    return buffer.getvalue().encode()


def proof(image,card,result):
    h,w=image.shape[:2]
    output=Image.new("RGB",(max(414,w),h+65),"#293536")
    output.paste(Image.fromarray(image),(0,0))
    draw=ImageDraw.Draw(output)
    c=card["settings"]
    draw.rectangle((c["left"],c["top"],c["right"],c["bottom"]),outline="#397e7e",width=2)
    if result["profile"]:
        draw.line((c["focus"],c["top"],c["focus"],c["bottom"]),fill="#c4884c",width=2)
    span=result["reading"]
    if span:
        a,b=span["end_y"]
        draw.line((c["focus"],a,c["focus"],b),fill="#ab5844",width=4)
    detail=(f'Longest dark impression: {span["width_px"]} px / {span["width_mm"]:.2f} mm'
            if span else "No recorded dark impression on this column")
    draw.text((14,h+14),f'Source column {c["focus"]}, threshold {c["cutoff"]}',fill="#e8eeeb")
    draw.text((14,h+35),detail,fill="#edc7a5")
    return png(output)


def package(card,original,image,result):
    memory=BytesIO()
    with ZipFile(memory,"w",ZIP_DEFLATED) as stream:
        stream.writestr("packet.png",original)
        stream.writestr("reading.json",json.dumps({"card":card,"reading":result["reading"]},ensure_ascii=False,indent=2))
        stream.writestr("seam-profile.csv",trace_table(result))
        stream.writestr("seam-proof.png",proof(image,card,result))
    return memory.getvalue()
