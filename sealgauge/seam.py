"""Measure a real source cross-section of a photographed pressed seam."""

from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from image_lab.validation import Invalid, integer, number


LUMA = np.array([0.2126, 0.7152, 0.0722])
MAX_IMAGE_BYTES = 18 * 1024 * 1024


def png(image):
    stream = BytesIO()
    image.save(stream,format="PNG")
    return stream.getvalue()


def decode(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise Invalid("单张原片不得超过18 MiB")
    try:
        with Image.open(BytesIO(data)) as photo:
            if photo.format not in {"PNG","JPEG","TIFF","BMP","WEBP"} or getattr(photo,"n_frames",1) != 1:
                raise Invalid("请选择单帧 PNG、JPEG、TIFF、BMP 或 WebP")
            if min(photo.size) < 32 or max(photo.size) > 2600 or math.prod(photo.size) > 4_000_000:
                raise Invalid("原片边长须在32–2600 px内，总像素不超过400万")
            normal=ImageOps.exif_transpose(photo).convert("RGB")
            return normal.size,png(normal)
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError) as error:
        raise Invalid("原片无法解码") from error


def pixels(path):
    with Image.open(path) as photo:
        return np.asarray(photo.convert("RGB"))


def settings(value,size):
    w,h = size
    left = integer(value.get("left"),"左界 X",0,w-1)
    top = integer(value.get("top"),"上界 Y",0,h-1)
    return {"left":left,"right":integer(value.get("right"),"右界 X",left,w-1),
            "top":top,"bottom":integer(value.get("bottom"),"下界 Y",top,h-1),
            "focus":integer(value.get("focus"),"截线 X",0,w-1),
            "cutoff":integer(value.get("cutoff"),"压痕亮度上限",1,254),
            "mm_per_px":number(value.get("mm_per_px"),"标尺",0.001,500),
            "nominal_mm":number(value.get("nominal_mm",3),"标称间距",0.001,500),
            "tolerance_mm":number(value.get("tolerance_mm",0.3),"允许偏差",0,500)}


def defaults(size):
    w,h = size
    return settings({"left":w//6,"right":w*5//6,"top":h*2//5,"bottom":h*3//5,
                     "focus":w//2,"cutoff":151,"mm_per_px":1,
                     "nominal_mm":3,"tolerance_mm":.3},size)


def point(image,x,y,config):
    rgb=image[y,x]
    brightness=float(rgb@LUMA)
    inside=config["left"]<=x<=config["right"] and config["top"]<=y<=config["bottom"]
    return {"x":x,"y":y,"rgb":[int(v) for v in rgb],"brightness":brightness,
            "pressed":bool(brightness<=config["cutoff"]) if inside else None}


def longest_span(marked):
    start=None
    best=(0,-1)
    for i,value in enumerate(marked):
        if value and start is None:
            start=i
        if not value:
            start=None
        if start is not None and i-start+1 > best[1]-best[0]+1:
            best=(start,i)
    return best


def profile(image,config):
    x=config["focus"]
    if not config["left"]<=x<=config["right"]:
        return []
    y0,y1=config["top"],config["bottom"]
    light=image[y0:y1+1,x]@LUMA
    return [{"x":x,"y":y,"brightness":float(value),"pressed":bool(value<=config["cutoff"])}
            for y,value in zip(range(y0,y1+1),light)]


def coverage(image,config):
    x0,x1,y0,y1=(config[k] for k in ("left","right","top","bottom"))
    light=image[y0:y1+1,x0:x1+1]@LUMA
    measured=np.count_nonzero(light<=config["cutoff"],axis=0)
    return [{"x":x,"pressed_px":int(count)} for x,count in zip(range(x0,x1+1),measured)]


MIN_RUN=3        # 每道至少三个连续真实暗像素
BREAK_COST=2.0   # 相邻原列同一道 y 段不重叠的代价（以允许偏差为单位）


def overlap(first,second):
    return max(first[0],second[0])<=min(first[1],second[1])


def chain(columns):
    """Pick at most one pair per column, favoring nominal spacing and unbroken tracks."""
    chosen=[None]*len(columns)
    start=0
    while start<len(columns):
        if not columns[start]:
            start+=1
            continue
        stop=start
        while stop+1<len(columns) and columns[stop+1]:
            stop+=1
        states=[]
        for candidates in columns[start:stop+1]:
            level=[]
            for candidate in candidates:
                if not states:
                    level.append((candidate["cost"],None,candidate))
                    continue
                total,back=min(((previous[0]+(0 if overlap(previous[2]["upper"],candidate["upper"])
                                              and overlap(previous[2]["lower"],candidate["lower"]) else BREAK_COST),index)
                                for index,previous in enumerate(states[-1])),key=lambda item:item[0])
                level.append((candidate["cost"]+total,back,candidate))
            states.append(level)
        index=min(range(len(states[-1])),key=lambda i:states[-1][i][0])
        for offset in range(len(states)-1,-1,-1):
            _,back,candidate=states[offset][index]
            chosen[start+offset]=candidate
            index=back
        start=stop+1
    return chosen


def seam_tracks(image,config):
    """Find the real upper/lower dark pair in every source column; empty columns stay empty."""
    x0,x1,y0,y1=(config[k] for k in ("left","right","top","bottom"))
    cutoff=config["cutoff"]
    box=image[y0:y1+1,x0:x1+1]
    light=box@LUMA
    dark=(light<=cutoff)&(box.min(axis=2)>=1)&(box.max(axis=2)<=254)
    edges=np.diff(np.pad(dark.astype(np.int8),((1,1),(0,0))),axis=0)
    starts,ends=edges==1,edges==-1
    nominal_px=config["nominal_mm"]/config["mm_per_px"]
    tolerance_px=config["tolerance_mm"]/config["mm_per_px"]
    scale=max(tolerance_px,1e-9)
    columns=[]
    for i in range(x1-x0+1):
        segments=[(int(s),int(e-1)) for s,e in zip(np.flatnonzero(starts[:,i]),np.flatnonzero(ends[:,i]))
                  if e-s>=MIN_RUN]
        candidates=[]
        for first in range(len(segments)):
            for second in range(first+1,len(segments)):
                (u0,u1),(l0,l1)=segments[first],segments[second]
                if not np.any(light[u1+1:l0,i]>cutoff):
                    continue
                spacing=(l0+l1-u0-u1)/2
                deviation=abs(spacing-nominal_px)
                if deviation<=tolerance_px+1e-9:
                    candidates.append({"upper":(u0+y0,u1+y0),"lower":(l0+y0,l1+y0),"spacing":spacing,
                                       "deviation":deviation,"cost":deviation/scale})
        columns.append(candidates)
    return chain(columns)


def segment_json(segment,config):
    top,bottom=segment
    width=bottom-top+1
    return {"end_y":[top,bottom],"center_y":(top+bottom)/2,
            "width_px":width,"width_mm":width*config["mm_per_px"]}


def pair_json(candidate,config):
    return {"spacing_px":candidate["spacing"],"spacing_mm":candidate["spacing"]*config["mm_per_px"],
            "deviation_mm":candidate["deviation"]*config["mm_per_px"],
            "upper":segment_json(candidate["upper"],config),"lower":segment_json(candidate["lower"],config)}


def report(image,config):
    values=profile(image,config)
    a,b=longest_span([p["pressed"] for p in values])
    width=b-a+1
    reading=None if width==0 else {"width_px":width,"width_mm":width*config["mm_per_px"],
                                    "end_y":[values[a]["y"],values[b]["y"]]}
    tracks=seam_tracks(image,config)
    pair=None
    if config["left"]<=config["focus"]<=config["right"]:
        candidate=tracks[config["focus"]-config["left"]]
        if candidate is not None:
            pair=pair_json(candidate,config)
    return {"reading":reading,"pair":pair,"profile":values,"coverage":coverage(image,config),
            "tracks":[None if candidate is None else
                      {"spacing_px":candidate["spacing"],"spacing_mm":candidate["spacing"]*config["mm_per_px"],
                       "upper":list(candidate["upper"]),"lower":list(candidate["lower"])}
                      for candidate in tracks],
            "focus":config["focus"]}


def practice():
    w,h=752,456
    cfg=settings({"left":111,"right":659,"top":200,"bottom":285,
                  "focus":333,"cutoff":151,"mm_per_px":.4,
                  "nominal_mm":12.8,"tolerance_mm":1.2},(w,h))
    yy,xx=np.indices((h,w))
    sheet=(xx>86)&(xx<675)&(yy>98)&(yy<361)
    base=215+6*np.cos(xx/97)-yy*.025 + 1.6*np.sin(xx*.38+yy*.77)
    center=240 + 3.3*np.sin((xx-88)/67)
    taper=6.2-2.4*np.exp(-((xx-479)/33)**2)
    upper=np.clip(taper-np.abs(yy-(center-16)),0,1)
    fade=1-np.exp(-((xx-605)/16)**2)
    lower=np.clip(taper-np.abs(yy-(center+16)),0,1)*fade
    channels= (np.exp(-((xx-372-.22*(yy-235))/3.1)**2)+
        .85*np.exp(-((xx-541+.17*(yy-240))/2.3)**2)*np.exp(-((yy-234)/8.4)**2))
    stroke=(upper+lower)*(1-np.clip(channels,0,1))*sheet
    relief=7*np.cos(xx*.79)+5*np.sin(yy*.63+xx*.27)
    dashes=((xx%46)<26)&(xx>250)&(xx<540)
    middle=center+1.6*np.sin(xx/41)
    ink=(np.clip(2.4-np.abs(yy-(middle-4)),0,1)+
         np.clip(2.4-np.abs(yy-(middle+4)),0,1))*dashes*sheet
    band=[base+offset+sheet*tint-stroke*dark+stroke*relief-ink*print_dark
                            for offset,tint,dark,print_dark in [(7,-12,87,118),(4,1,79,110),(-6,7,69,96)]]
    photo=Image.fromarray(np.stack(band,axis=-1).clip(0,255).astype("uint8"),"RGB")
    draw=ImageDraw.Draw(photo)
    draw.rounded_rectangle((86,98,675,361),radius=14,outline="#879e9a",width=2)
    draw.line((95,109,667,109),fill="#eff7ef",width=2)
    draw.line((97,351,660,351),fill="#b9d1c9",width=2)
    draw.text((29,51),"DOUBLE-TRACK SEAM / SAMPLE A",fill="#505c57",font=ImageFont.load_default(size=17))
    draw.text((28,392),"SIMULATED PACKET PHOTO",fill="#52625a",font=ImageFont.load_default(size=11))
    draw.line((44,421,144,421),fill="#46554e",width=2)
    for x in range(44,145,10):
        draw.line((x,421,x,412 if (x-44)%50 else 407),fill="#46554e",width=2)
    draw.text((156,410),"40 mm",fill="#46554e",font=ImageFont.load_default(size=12))
    return photo.size,png(photo),cfg
