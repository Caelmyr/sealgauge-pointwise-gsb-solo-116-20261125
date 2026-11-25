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


def tracks_table(result,left):
    buffer=StringIO()
    writer=csv.writer(buffer)
    writer.writerow(("x_px","spacing_px","spacing_mm","upper_y0","upper_y1","lower_y0","lower_y1"))
    for offset,track in enumerate(result["tracks"]):
        if track is None:
            writer.writerow((left+offset,"","","","","",""))
        else:
            (u0,u1),(l0,l1)=track["upper"],track["lower"]
            writer.writerow((left+offset,f'{track["spacing_px"]:.2f}',f'{track["spacing_mm"]:.3f}',u0,u1,l0,l1))
    return buffer.getvalue().encode()


def proof(image,card,result):
    h,w=image.shape[:2]
    output=Image.new("RGB",(max(414,w),h+104),"#293536")
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
    pair=result["pair"]
    if pair:
        for segment,color in ((pair["upper"],"#4f86c6"),(pair["lower"],"#9a6fc0")):
            a,b=segment["end_y"]
            draw.line((c["focus"],a,c["focus"],b),fill=color,width=4)
            center=segment["center_y"]
            draw.line((c["focus"]-6,center,c["focus"]+6,center),fill=color,width=2)
    detail=(f'Longest dark impression: {span["width_px"]} px / {span["width_mm"]:.2f} mm'
            if span else "No recorded dark impression on this column")
    spacing=(f'Double-track spacing: {pair["spacing_px"]:.1f} px / {pair["spacing_mm"]:.2f} mm '
             f'(nominal {c["nominal_mm"]} +/- {c["tolerance_mm"]} mm)'
             if pair else "No valid double-track pair on this column")
    draw.text((14,h+12),f'Source column {c["focus"]}, threshold {c["cutoff"]}',fill="#e8eeeb")
    draw.text((14,h+33),detail,fill="#edc7a5")
    draw.text((14,h+54),spacing,fill="#b8d2ea")
    if pair:
        upper,lower=pair["upper"],pair["lower"]
        draw.text((14,h+75),f'Upper {upper["width_px"]} px / {upper["width_mm"]:.2f} mm at Y {upper["end_y"][0]}-{upper["end_y"][1]}, '
                             f'lower {lower["width_px"]} px / {lower["width_mm"]:.2f} mm at Y {lower["end_y"][0]}-{lower["end_y"][1]}',
                  fill="#d9cbe8")
    return png(output)


def package(card,original,image,result):
    memory=BytesIO()
    with ZipFile(memory,"w",ZIP_DEFLATED) as stream:
        stream.writestr("packet.png",original)
        stream.writestr("reading.json",json.dumps({"card":card,"reading":result["reading"],"pair":result["pair"]},ensure_ascii=False,indent=2))
        stream.writestr("seam-profile.csv",trace_table(result))
        stream.writestr("seam-tracks.csv",tracks_table(result,card["settings"]["left"]))
        stream.writestr("seam-proof.png",proof(image,card,result))
    return memory.getvalue()
