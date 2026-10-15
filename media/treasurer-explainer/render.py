"""Render the 30-second Treasurer explainer. No application or Stripe calls.

Requires Python, Pillow, NumPy, and FFmpeg. Run from any directory:
  python media/treasurer-explainer/render.py --preview
  python media/treasurer-explainer/render.py
Fonts download once; narration WAVs are supplied alongside this source.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import subprocess
import textwrap
import urllib.request
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
W, H, FPS, DURATION = 1920, 1080, 30, 30
BG = "#F5F6F4"
WHITE = "#FFFFFF"
INK = "#17211D"
MUTED = "#66706B"
LINE = "#DCE2DC"
GREEN = "#1D4A3A"
MINT = "#D7EBDD"
LIGHT = "#EAF2EE"
OK = "#1E7047"
AMBER = "#8F5B00"
PURPLE = "#4A4CA0"
RED = "#B42318"

# Start/end times give narration a short lead-in and breathing room.
SCENES = [
    (0.0, 4.0, "01-meet", "Meet Treasurer, a spending agent that explains every pound."),
    (4.0, 8.0, "02-why", "Teams need faster purchases, with clear limits and accountability."),
    (8.0, 15.0, "03-where", "In this demo, purchase emails arrive as inbox files, with payments simulated in Stripe test mode."),
    (15.0, 20.5, "04-how", "Treasurer extracts the request, recalls finance notes in Cognee, and checks written rules."),
    (20.5, 24.5, "05-outcomes", "It clears, seeks approval, holds, or declines."),
    (24.5, 30.0, "06-explain", "Ask why, and get the policy clause, evidence, and decision history."),
]
VOICE_START = 0.22


def fonts():
    ASSETS.mkdir(exist_ok=True)
    css_path = ASSETS / "fonts.css"
    if not css_path.exists():
        url = "https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500"
        css_path.write_bytes(urllib.request.urlopen(url, timeout=30).read())
    for block in css_path.read_text().split("@font-face"):
        family = re.search(r"font-family: '([^']+)'", block)
        weight = re.search(r"font-weight: (\d+)", block)
        source = re.search(r"src: url\(([^)]+)\)", block)
        if not (family and weight and source):
            continue
        name = family[1].lower().replace(" ", "-")
        path = ASSETS / f"{name}-{weight[1]}.ttf"
        if not path.exists():
            path.write_bytes(urllib.request.urlopen(source[1], timeout=30).read())
    for family in ("schibstedgrotesk", "jetbrainsmono"):
        path = ASSETS / f"{family}-OFL.txt"
        if not path.exists():
            url = f"https://raw.githubusercontent.com/google/fonts/main/ofl/{family}/OFL.txt"
            path.write_bytes(urllib.request.urlopen(url, timeout=30).read())


@lru_cache(maxsize=150)
def font(size, weight=400, mono=False):
    family = "jetbrains-mono" if mono else "schibsted-grotesk"
    if mono and weight >= 500:
        weight = 500
    return ImageFont.truetype(str(ASSETS / f"{family}-{weight}.ttf"), int(size))


def clamp(v):
    return max(0.0, min(1.0, v))


def ease(v):
    return 1 - (1 - clamp(v)) ** 3


def smooth(v):
    v = clamp(v)
    return v * v * (3 - 2 * v)


def lerp(a, b, v):
    return a + (b - a) * v


def text(im, xy, value, size=36, fill=INK, weight=400, mono=False, anchor="lt"):
    if "\n" in value:
        for i, row in enumerate(value.splitlines()):
            text(im,(xy[0],xy[1]+round(i*size*1.16)),row,size,fill,weight,mono,anchor)
        return
    ImageDraw.Draw(im).text(xy, value, font=font(size, weight, mono), fill=fill, anchor=anchor, stroke_width=0)


def box(im, bounds, fill=WHITE, outline=LINE, radius=16, width=2):
    ImageDraw.Draw(im).rounded_rectangle(tuple(round(v) for v in bounds), radius=radius, fill=fill, outline=outline, width=width)


def line(im, points, fill=LINE, width=3):
    ImageDraw.Draw(im).line(points, fill=fill, width=width, joint="curve")


def icon(im, kind, x, y, size=60, color=GREEN, width=4):
    d = ImageDraw.Draw(im)
    s = size
    if kind == "check":
        d.line([(x + .17*s, y + .52*s), (x + .40*s, y + .73*s), (x + .84*s, y + .24*s)], fill=color, width=width, joint="curve")
    elif kind == "cross":
        d.line([(x+.24*s,y+.24*s),(x+.76*s,y+.76*s)],fill=color,width=width)
        d.line([(x+.76*s,y+.24*s),(x+.24*s,y+.76*s)],fill=color,width=width)
    elif kind == "pause":
        d.rounded_rectangle((x+.27*s,y+.22*s,x+.41*s,y+.78*s),radius=3,fill=color)
        d.rounded_rectangle((x+.59*s,y+.22*s,x+.73*s,y+.78*s),radius=3,fill=color)
    elif kind == "person":
        d.ellipse((x+.36*s,y+.14*s,x+.64*s,y+.42*s),outline=color,width=width)
        d.arc((x+.18*s,y+.44*s,x+.82*s,y+1.04*s),180,360,fill=color,width=width)
        d.line((x+.18*s,y+.74*s,x+.18*s,y+.84*s),fill=color,width=width)
        d.line((x+.82*s,y+.74*s,x+.82*s,y+.84*s),fill=color,width=width)
    elif kind == "mail":
        box(im,(x,y+.15*s,x+s,y+.85*s),fill=None,outline=color,radius=int(s*.06),width=width)
        d.line([(x+.02*s,y+.20*s),(x+.5*s,y+.55*s),(x+.98*s,y+.20*s)],fill=color,width=width,joint="curve")
    elif kind == "document":
        box(im,(x+.18*s,y+.08*s,x+.82*s,y+.92*s),fill=None,outline=color,radius=4,width=width)
        for yy, ww in ((.33,.39),(.50,.39),(.67,.25)):
            d.line((x+.30*s,y+yy*s,x+(.30+ww)*s,y+yy*s),fill=color,width=width)
    elif kind == "memory":
        pts=[(.18,.52),(.42,.20),(.80,.30),(.68,.77),(.30,.82)]
        for a,b in ((0,1),(1,2),(2,3),(3,4),(4,0),(1,3)):
            d.line((x+pts[a][0]*s,y+pts[a][1]*s,x+pts[b][0]*s,y+pts[b][1]*s),fill=color,width=width)
        for xx,yy in pts:
            d.ellipse((x+xx*s-6,y+yy*s-6,x+xx*s+6,y+yy*s+6),fill=color)


def arrow(im, x1, y1, x2, y2, progress=1, fill=GREEN, width=3):
    xx = lerp(x1,x2,clamp(progress))
    yy = lerp(y1,y2,clamp(progress))
    line(im,[(x1,y1),(xx,yy)],fill,width)
    if progress >= .99:
        angle=math.atan2(y2-y1,x2-x1)
        pts=[]
        for offset in (-.6,.6):
            pts.append((x2-16*math.cos(angle+offset),y2-16*math.sin(angle+offset)))
        line(im,[pts[0],(x2,y2),pts[1]],fill,width)


def animate(base, draw_fn, t, delay=0, duration=.52, dx=0, dy=26):
    p=ease((t-delay)/duration)
    if p<=0:
        return
    layer=Image.new("RGBA",(W,H))
    draw_fn(layer)
    if p<1:
        layer.putalpha(layer.getchannel("A").point(lambda a:round(a*p)))
    base.alpha_composite(layer,(round(dx*(1-p)),round(dy*(1-p))))


def label(im, value, dark=False):
    text(im,(110,185),value,25,MINT if dark else GREEN,600,True)


def virtual_card(im,x,y,w=575,h=355):
    target=im
    position=(x,y)
    im=Image.new("RGBA",(w,h))
    x=y=0
    box(im,(0,0,w-1,h-1),"#163B2F","#527662",28)
    d=ImageDraw.Draw(im)
    # The actual product card's ring motif, kept inside the card.
    for r in range(95,690,28):
        d.arc((x+w-100-r,y-150-r,x+w-100+r,y-150+r),15,178,fill="#38644F",width=2)
    text(im,(x+35,y+30),"Treasurer",33,LIGHT,600)
    text(im,(x+w-35,y+37),"GBP",22,MINT,500,True,"rt")
    box(im,(x+36,y+102,x+101,y+149),"#CABA81","#CABA81",9)
    line(im,[(x+68,y+103),(x+68,y+149)],GREEN,2)
    line(im,[(x+38,y+124),(x+100,y+124)],GREEN,2)
    text(im,(x+35,y+181),"£120",88,WHITE,600)
    text(im,(x+38,y+h-57),"Figma · software",25,MINT)
    text(im,(x+w-36,y+h-55),"TEST",21,MINT,500,True,"rt")
    mask=Image.new("L",(w,h))
    ImageDraw.Draw(mask).rounded_rectangle((0,0,w-1,h-1),28,fill=255)
    im.putalpha(mask)
    target.alpha_composite(im,position)


def scene_what(t):
    im=Image.new("RGBA",(W,H),GREEN)
    label(im,"01 / WHAT",True)
    animate(im,lambda a:text(a,(110,285),"A spending agent.",107,WHITE,600),t,.05)
    animate(im,lambda a:text(a,(110,425),"Every pound\nexplained.",85,MINT,400),t,.35,dy=35)
    animate(im,lambda a:text(a,(114,675),"Policy → payment → proof",32,LIGHT,400),t,.7)
    card=Image.new("RGBA",(W,H))
    virtual_card(card,1165,285)
    animate(im,lambda a:a.alpha_composite(card),t,.22,.7,dx=80,dy=0)
    def receipt(a):
        box(a,(1220,650,1692,809),LIGHT,LIGHT,14)
        for j,(left,right) in enumerate((("C1","Within £500 cap"),("C2","Software allowed"),("C7","Memory clear"))):
            yy=673+j*42
            icon(a,"check",1240,yy,24,OK,3)
            text(a,(1280,yy),left,23,GREEN,500,True)
            text(a,(1350,yy),right,23,INK)
    animate(im,receipt,t,1.0,.6,dy=-45)
    return im


def scene_why(t):
    im=Image.new("RGBA",(W,H),BG)
    label(im,"02 / WHY")
    animate(im,lambda a:text(a,(110,275),"Move faster.",109,INK,600),t,.02)
    animate(im,lambda a:text(a,(110,405),"Stay accountable.",90,GREEN,600),t,.22)
    animate(im,lambda a:text(a,(114,650),"£500 per purchase",35,INK,400),t,.5)
    animate(im,lambda a:text(a,(114,710),"£2,000 per month",35,MUTED,400),t,.64)
    xs=[1120,1360,1600]
    for j,(x,title,sub) in enumerate(zip(xs,["LIMITS","APPROVAL","EVIDENCE"],["Caps enforced","A named person","A clear record"])):
        def gate(a,x=x,title=title,sub=sub,j=j):
            box(a,(x,355,x+155,640),LIGHT,LINE,18)
            line(a,[(x+25,618),(x+25,390),(x+130,390),(x+130,618)],GREEN,6)
            text(a,(x+77,680),title,23,GREEN,500,True,"mt")
            text(a,(x+77,722),sub,22,MUTED,400,False,"mt")
        animate(im,gate,t,.25+j*.15,.45,dy=30)
    p=smooth((t-.8)/2.3)
    x=lerp(1100,1790,p)
    line(im,[(1100,540),(1790,540)],"#A6C1AE",3)
    d=ImageDraw.Draw(im)
    d.ellipse((x-43,497,x+43,583),fill=GREEN)
    text(im,(x,510),"£",49,WHITE,600,False,"mt")
    return im


def scene_where(t):
    im=Image.new("RGBA",(W,H),BG)
    label(im,"03 / WHERE")
    animate(im,lambda a:text(a,(110,265),"From inbox to Stripe.",99,INK,600),t,.02)
    xs=[395,965,1535]
    arrow(im,540,552,830,552,ease((t-.8)/.65),"#A3BAAC",4)
    arrow(im,1100,552,1390,552,ease((t-1.35)/.65),"#A3BAAC",4)
    def inbox(a):
        box(a,(245,425,545,665),WHITE,LINE,20)
        icon(a,"mail",310,458,170,GREEN,5)
    animate(im,inbox,t,.25)
    def treasurer(a):
        box(a,(855,440,1075,660),GREEN,GREEN,35)
        text(a,(965,451),"T",158,WHITE,600,False,"mt")
    animate(im,treasurer,t,.55)
    def stripe(a):
        box(a,(1380,455,1690,645),WHITE,LINE,20)
        text(a,(1410,488),"stripe",58,INK,700)
        box(a,(1412,570,1498,606),"#ECECF9","#ECECF9",8)
        text(a,(1455,575),"TEST",20,PURPLE,500,True,"mt")
        icon(a,"check",1600,570,37,OK,4)
    animate(im,stripe,t,.85)
    for j,(x,title,sub) in enumerate(zip(xs,["INBOX FILES","TREASURER","STRIPE TEST MODE"],["Purchase requests","Policy + memory","Authorize + capture"])):
        animate(im,lambda a,x=x,title=title,sub=sub:(text(a,(x,715),title,26,GREEN,500,True,"mt"),text(a,(x,760),sub,27,MUTED,400,False,"mt")),t,.45+j*.25)
    p=smooth((t-1.8)/2.8)
    x=lerp(580,1330,p)
    if t>1.8:
        box(im,(x-102,505,x+102,556),GREEN,GREEN,25)
        text(im,(x,514),"Figma · £120",22,WHITE,600,False,"mt")
    return im


def scene_how(t):
    im=Image.new("RGBA",(W,H),BG)
    label(im,"04 / HOW")
    animate(im,lambda a:text(a,(110,255),"Read. Recall. Check.",99,INK,600),t,.02)
    def request(a):
        box(a,(110,420,595,750),WHITE,LINE,18)
        icon(a,"mail",140,452,42,GREEN,3)
        text(a,(200,454),"PURCHASE REQUEST",23,MUTED,500,True)
        text(a,(145,527),"Figma seat for £120",38,INK,600)
        line(a,[(145,590),(560,590)],LINE,2)
        text(a,(145,625),"Merchant",24,MUTED)
        text(a,(560,620),"Figma",29,INK,600,False,"rt")
        text(a,(145,682),"Amount",24,MUTED)
        text(a,(560,677),"£120",29,INK,600,False,"rt")
    animate(im,request,t,.2)
    def memory(a):
        box(a,(760,415,1245,565),LIGHT,LINE,16)
        icon(a,"memory",785,451,52,GREEN,3)
        text(a,(865,445),"Cognee memory",32,GREEN,600)
        text(a,(865,498),"C7 · No merchant hold",25,MUTED)
    animate(im,memory,t,.65)
    def policy(a):
        box(a,(760,605,1245,755),WHITE,LINE,16)
        icon(a,"document",785,640,52,GREEN,3)
        text(a,(865,630),"Written policy",32,INK,600)
        text(a,(865,683),"C1 · £120 ≤ £500",25,MUTED)
    animate(im,policy,t,.95)
    arrow(im,605,530,747,490,ease((t-.7)/.5),"#98B2A1",3)
    arrow(im,605,650,747,680,ease((t-1)/.5),"#98B2A1",3)
    arrow(im,1257,490,1430,565,ease((t-1.25)/.5),"#98B2A1",3)
    arrow(im,1257,680,1430,605,ease((t-1.4)/.5),"#98B2A1",3)
    def verdict(a):
        box(a,(1450,480,1785,690),GREEN,GREEN,20)
        icon(a,"check",1560,508,100,WHITE,7)
        text(a,(1617,625),"POLICY CLEARED",24,LIGHT,500,True,"mt")
    animate(im,verdict,t,1.65)
    animate(im,lambda a:text(a,(960,804),"AI extracts. Python decides.",32,GREEN,600,False,"mt"),t,2)
    return im


def scene_outcomes(t):
    im=Image.new("RGBA",(W,H),BG)
    label(im,"04 / HOW · THE DECISION")
    animate(im,lambda a:text(a,(110,260),"Every request gets a decision.",86,INK,600),t,.02)
    data=[
        ("Cleared","Figma","£120","Within policy",OK,"#E5F3EB","check","C1 / C2 / C7"),
        ("Needs approval","Google Ads","£350","Above £200",AMBER,"#FBF0D9","person","C3"),
        ("Held","Meta Ads","£150","Under review",PURPLE,"#ECECF9","pause","C7"),
        ("Declined","Pret","£42.50","Category blocked",RED,"#FCEBE9","cross","C2"),
    ]
    for j,(status,merchant,amount,reason,color,tint,kind,clauses) in enumerate(data):
        x=110+j*431
        def tile(a,x=x,status=status,merchant=merchant,amount=amount,reason=reason,color=color,tint=tint,kind=kind,clauses=clauses):
            box(a,(x,415,x+407,785),WHITE,LINE,18)
            box(a,(x+25,440,x+87,502),tint,tint,14)
            icon(a,kind,x+33,448,46,color,4)
            text(a,(x+25,530),status,31,color,600)
            text(a,(x+25,586),merchant,29,INK,400)
            text(a,(x+25,638),amount,57,INK,600)
            text(a,(x+25,724),reason,24,MUTED)
            text(a,(x+382,460),clauses,18,color,500,True,"rt")
        animate(im,tile,t,.20+j*.17,.48,dy=35)
    return im


def scene_explain(t):
    im=Image.new("RGBA",(W,H),BG)
    label(im,"04 / HOW · THE EXPLANATION")
    animate(im,lambda a:text(a,(110,265),'“Why did you pay this?”',93,INK,600),t,.02)
    def proof(a):
        box(a,(110,420,1810,775),WHITE,LINE,20)
        text(a,(148,450),"FIGMA · £120",24,GREEN,500,True)
        text(a,(1770,452),"ILLUSTRATIVE RECORD",20,MUTED,400,True,"rt")
        line(a,[(148,503),(1770,503)],LINE,2)
        for j,(title,big,sub,kind) in enumerate([
            ("THE RULE","C1 · £500 cap","Policy clause recorded","document"),
            ("THE EVIDENCE","£120 · Figma","Merchant, amount, memory","check"),
            ("THE HISTORY","Decision + Stripe ID","A recorded audit trail","memory"),
        ]):
            x=148+j*555
            icon(a,kind,x,535,45,GREEN,3)
            text(a,(x+65,545),title,22,MUTED,500,True)
            text(a,(x,620),big,37,INK,600)
            text(a,(x,687),sub,25,MUTED)
            if j<2:
                line(a,[(x+510,538),(x+510,732)],LINE,2)
    animate(im,proof,t,.35)
    if t>4.0:
        p=ease((t-4.0)/.6)
        overlay=Image.new("RGBA",(W,H))
        ImageDraw.Draw(overlay).rectangle((0,0,round(W*p),H),fill=GREEN)
        im.alpha_composite(overlay)
        if p>.85:
            q=ease((t-4.4)/.4)
            def end(a):
                box(a,(902,255,1018,371),LIGHT,LIGHT,23)
                text(a,(960,263),"T",87,GREEN,600,False,"mt")
                text(a,(960,421),"Treasurer",110,WHITE,600,False,"mt")
                text(a,(960,575),"Every pound explained.",58,MINT,400,False,"mt")
                text(a,(960,695),"POLICY → PAYMENT → PROOF",25,LIGHT,500,True,"mt")
            layer=Image.new("RGBA",(W,H))
            end(layer)
            layer.putalpha(layer.getchannel("A").point(lambda a:round(a*q)))
            im.alpha_composite(layer)
    return im


DRAWERS=[scene_what,scene_why,scene_where,scene_how,scene_outcomes,scene_explain]


@lru_cache(maxsize=6)
def voice_duration(name):
    with wave.open(str(ROOT/"audio"/f"{name}.wav"),"rb") as f:
        return f.getnframes()/f.getframerate()


def chrome(im,t,index):
    dark=index==0 or (index==5 and t>=29.08)
    primary=LIGHT if dark else INK
    secondary="#B9CFC0" if dark else MUTED
    box(im,(110,58,157,105),LIGHT if dark else GREEN,LIGHT if dark else GREEN,11)
    text(im,(133.5,61),"T",35,GREEN if dark else WHITE,600,False,"mt")
    text(im,(175,65),"Treasurer",31,primary,600)
    text(im,(1810,67),"ILLUSTRATED DEMO · STRIPE TEST MODE",21,secondary,500,True,"rt")
    line(im,[(110,137),(1810,137)],"#416955" if dark else LINE,2)
    labels=["WHAT","WHY","WHERE","HOW"]
    active=min(index,3)
    for j,value in enumerate(labels):
        x=110+j*230
        text(im,(x,1030),f"0{j+1}  {value}",19,primary if j==active else secondary,500,True)
        if j==active:
            line(im,[(x,1010),(x+160,1010)],MINT if dark else GREEN,3)
    text(im,(1810,1030),f"{min(30,int(t)):02d} / 30 SEC",19,secondary,400,True,"rt")
    # Thin playback rule provides continuity between the six compositions.
    line(im,[(110,1071),(1810,1071)],"#416955" if dark else LINE,2)
    line(im,[(110,1071),(110+1700*clamp(t/30),1071)],MINT if dark else GREEN,3)


def caption(im,t,index):
    start,end,name,words=SCENES[index]
    st=start+VOICE_START
    en=st+voice_duration(name)
    if not(st<=t<=en+.12):
        return
    rows=textwrap.wrap(words,72)
    height=70 if len(rows)==1 else 111
    widths=[font(35,400).getlength(row) for row in rows]
    ww=max(widths)+70
    x=(W-ww)/2
    y=927-(height-70)/2
    box(im,(x,y,x+ww,y+height),INK,INK,14)
    for j,row in enumerate(rows):
        text(im,(960,y+15+j*44),row,35,WHITE,400,False,"mt")


def frame(t):
    index=next((i for i,(start,end,*_) in enumerate(SCENES) if start<=t<end),5)
    start=SCENES[index][0]
    local=t-start
    im=DRAWERS[index](local)
    # Fast editorial dissolve, preserving enough hold time to read each scene.
    if index>0 and local<.23:
        prev=DRAWERS[index-1](SCENES[index-1][1]-SCENES[index-1][0]-.02)
        im=Image.blend(prev,im,smooth(local/.23))
    chrome(im,t,index)
    caption(im,t,index)
    return im.convert("RGB")


def timestamp(t):
    ms=round(t*1000)
    return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"


def build_audio():
    sr=48000
    count=sr*DURATION
    speech=np.zeros(count,dtype=np.float64)
    srt=[]
    for i,(start,end,name,words) in enumerate(SCENES):
        pcm=subprocess.check_output(["ffmpeg","-v","error","-i",str(ROOT/"audio"/f"{name}.wav"),"-f","f32le","-ac","1","-ar",str(sr),"pipe:1"])
        samples=np.frombuffer(pcm,dtype="<f4").astype(np.float64)
        at=round((start+VOICE_START)*sr)
        speech[at:at+len(samples)]+=samples
        en=start+VOICE_START+len(samples)/sr
        if en>end-.05:
            raise ValueError(f"Narration {name} crosses scene end: {en} > {end}")
        srt.append(f"{i+1}\n{timestamp(start+VOICE_START)} --> {timestamp(en)}\n"+"\n".join(textwrap.wrap(words,72)))
    # Original, quietly pulsing soundtrack. All oscillators are generated here.
    music=np.zeros(count,dtype=np.float64)
    for n in range(50):
        at=round(n*.6*sr)
        if at>=count:
            break
        dur=1.7
        tt=np.arange(round(dur*sr))/sr
        frequency=[146.832,220,293.665,329.628,220][n%5]
        envelope=(1-np.exp(-tt*30))*np.exp(-tt*3.5)
        note=(np.sin(2*np.pi*frequency*tt)+.28*np.sin(2*np.pi*2*frequency*tt))*envelope
        end=min(count,at+len(note))
        music[at:end]+=.017*note[:end-at]
        if n%2==0:
            kick=np.sin(2*np.pi*62*tt)*np.exp(-tt*18)
            music[at:end]+=.010*kick[:end-at]
    # Soft scene-change taps give the graphic cuts a tactile cue.
    for start,*_ in SCENES[1:]:
        at=round(start*sr)
        tt=np.arange(round(.22*sr))/sr
        tap=(np.sin(2*np.pi*520*tt)+.3*np.sin(2*np.pi*1040*tt))*np.exp(-tt*30)
        music[at:at+len(tap)]+=.027*tap
    # Gentle stereo spread; narration stays centered and clear.
    peak=max(.01,float(np.max(np.abs(speech))))
    speech=speech/peak*.72
    fade=np.minimum(1,np.arange(count)/(.6*sr))*np.minimum(1,(count-1-np.arange(count))/(.8*sr))
    left=(speech+music)*fade
    right=(speech+np.roll(music,round(.008*sr)))*fade
    stereo=np.stack([left,right],axis=1)
    data=(np.clip(stereo,-.98,.98)*32767).astype("<i2")
    with wave.open(str(ROOT/"soundtrack.wav"),"wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(data.tobytes())
    (ROOT/"treasurer-30s.srt").write_text("\n\n".join(srt)+"\n",encoding="utf-8")


def preview():
    times=[2.1,6.5,11.8,18.7,23.1,27.2,29.6]
    thumbs=[]
    out=ROOT/"preview"
    out.mkdir(exist_ok=True)
    for i,t in enumerate(times):
        im=frame(t)
        im.save(out/f"scene-{i+1:02}.png")
        im.thumbnail((640,360),Image.Resampling.LANCZOS)
        thumbs.append(im)
    sheet=Image.new("RGB",(1280,4*410),"#E3E9E3")
    for i,im in enumerate(thumbs):
        x=(i%2)*640
        y=(i//2)*410
        sheet.paste(im,(x,y))
        text(sheet,(x+20,y+375),f"{times[i]:04.1f}s",22,GREEN,500,True)
    sheet.save(ROOT/"storyboard.jpg",quality=94)
    print(f"Storyboard: {ROOT/'storyboard.jpg'}",flush=True)


def render():
    output=ROOT/"treasurer-30s.mp4"
    build_audio()
    command=["ffmpeg","-y","-hide_banner","-loglevel","warning","-f","rawvideo","-vcodec","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r",str(FPS),"-i","pipe:0","-i",str(ROOT/"soundtrack.wav"),"-c:v","libx264","-preset","medium","-crf","19","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-af","loudnorm=I=-16:TP=-1.5:LRA=7","-ar","48000","-t",str(DURATION),"-movflags","+faststart",str(output)]
    with (ROOT/"render.log").open("w",encoding="utf-8") as log:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=log)
        try:
            for n in range(FPS*DURATION):
                process.stdin.write(frame(n/FPS).tobytes())
                if n%90==0:
                    print(f"Rendering {n//FPS:02d}/{DURATION}s",flush=True)
        finally:
            process.stdin.close()
        if process.wait()!=0:
            raise RuntimeError((ROOT/"render.log").read_text())
    print(f"Video: {output}",flush=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--preview",action="store_true")
    args=p.parse_args()
    fonts()
    if args.preview:
        preview()
    else:
        render()


if __name__=="__main__":
    main()
