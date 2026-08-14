"""Cinematic intro generator for the MP4 merge export.

Renders one of four trailer-style openers that the export route prepends to a
merge, all sharing the same Pillow title card, color options, duration clamp,
fade-from-black envelope (the merge's head-trim detection exempts the intro)
and encode signature (canvas/fps/silent-audio matched to the merge target so a
uniform clip set keeps its lossless stream-copy concat):

- ``mosaic``  — the original: darkened/blurred clip backdrop, bordered grid of
  up to 9 mini-clips fading in staggered, title + frosted subtitle pill.
- ``epic``    — up to 3 clips full-bleed with a slow zoompan push (alternating
  in/out), crossfaded, cinematic letterbox bars with an accent hairline; the
  title drifts up as it fades in.
- ``cascade`` — a wall of bordered tiles in 3-4 columns scrolling vertically at
  different speeds/directions (parallax) over near-black; title on a frosted
  full-width band with accent hairlines.
- ``prism``   — up to 2 clips folded into a seamless 4-way mirror kaleidoscope
  (mandala centered on the canvas) that slowly rotates and pushes in, crossfaded;
  oversized tracked typography with thin accent rules on a frosted glass band,
  uppercase subtitle.

Implementation notes carried over from the original single-style version:
- No ImageMagick. The title card is drawn with Pillow (already a hard dep) —
  which also sidesteps ffmpeg drawtext's escaping pitfalls for user-typed text.
- The canvas is the MERGE TARGET (size/fps decided by the caller from the real
  clip set), not hardcoded — portrait and square merges get portrait layouts.
- Per-style small pre-encodes (minis / heroes / columns) collapse into ONE
  full-res compose pass (loops via -stream_loop, fades/overlays/title/grade/
  vignette in a single filter graph).
"""

from __future__ import annotations

import math
import random
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STYLES = ("mosaic", "epic", "cascade", "prism")

DEFAULTS = {
    "title": "",
    "subtitle": "",
    "title_color": "#D4AF37",
    "stroke_color": "#8B6914",
    "subtitle_color": "#E8D5A3",
    "border_color": "#D4AF37",
    "duration": 12.0,
    "style": "mosaic",
}

# Outer dark frame around each tile's colored border (spec's #1a1a1a).
_OUTER_COLOR = "1a1a1a"
# Cascade's wall background — near-black so intra-column gaps (padded into the
# column encodes with this exact color) are indistinguishable from the canvas.
_WALL_BG = "0d0d10"

_FONTS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Docker (fonts-dejavu-core)
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_FONTS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _run(cmd: list[str], what: str, timeout: int = 1800) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-400:] or f"ffmpeg failed ({what})")


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return max(0.0, float((out.stdout or "0").strip()))
    except Exception:
        return 0.0


def _hex6(color: str, fallback: str) -> str:
    h = str(color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return h.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", h) else fallback.lstrip("#").upper()


def _rgb(hex6: str) -> tuple[int, int, int]:
    return (int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16))


def _even(v: float) -> int:
    return int(v) - int(v) % 2


def _find_font(candidates: list[str]) -> str | None:
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def _grid_layout(n: int, W: int, H: int):
    """Tile geometry for n clips on a WxH canvas. Tiles share the canvas aspect
    (portrait merges get portrait tiles); a partial last row is centered.
    Returns (content_w, content_h, border_w, outer_w, positions) where positions
    are the top-left corners of each BORDERED tile."""
    a = math.ceil(math.sqrt(n))
    b = math.ceil(n / a)
    cols, rows = (b, a) if H > W else (a, b)

    unit = min(W, H)
    margin = round(unit * 0.05)
    gap = round(unit * 0.035)
    border = max(2, round(unit * 0.005))
    outer = max(1, round(border / 2))
    frame = border + outer  # per-side chrome around the content

    avail_w = W - 2 * margin - (cols - 1) * gap
    avail_h = H - 2 * margin - (rows - 1) * gap
    cw = avail_w // cols - 2 * frame
    ch = avail_h // rows - 2 * frame
    # Constrain content to the canvas aspect so tiles mirror the footage shape.
    if cw * H > ch * W:
        cw = round(ch * W / H)
    else:
        ch = round(cw * H / W)
    cw, ch = max(32, cw - cw % 2), max(32, ch - ch % 2)

    tw, th = cw + 2 * frame, ch + 2 * frame
    grid_h = rows * th + (rows - 1) * gap
    y0 = (H - grid_h) // 2
    positions: list[tuple[int, int]] = []
    for r in range(rows):
        in_row = min(cols, n - r * cols)
        row_w = in_row * tw + (in_row - 1) * gap
        x0 = (W - row_w) // 2
        for c in range(in_row):
            positions.append((x0 + c * (tw + gap), y0 + r * (th + gap)))
    return cw, ch, border, outer, positions


def _extract_mini(src: Path, dst: Path, cw: int, ch: int, border: int, outer: int,
                  border_color: str, fps: str) -> None:
    """One small encode: seek into the clip, cover-crop to the tile size, bake the
    colored + dark borders in. Looping to full intro length happens at compose
    time via -stream_loop, so the mini stays a ~3s file."""
    dur = _probe_duration(src)
    start = min(5.0, max(0.0, dur * 0.25))
    length = min(3.0, max(0.8, dur - start - 0.05)) if dur > 1.0 else 3.0
    frame = border + outer
    vf = (
        f"scale=iw*sar:ih,scale={cw}:{ch}:force_original_aspect_ratio=increase,"
        f"crop={cw}:{ch},setsar=1,fps={fps},"
        f"pad={cw + 2 * border}:{ch + 2 * border}:{border}:{border}:color=0x{border_color},"
        f"pad={cw + 2 * frame}:{ch + 2 * frame}:{outer}:{outer}:color=0x{_OUTER_COLOR},"
        f"format=yuv420p"
    )
    _run(
        ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}",
         "-vf", vf, "-an", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", str(dst)],
        f"intro mini {dst.name}", timeout=300,
    )


def _title_card(path: Path, W: int, H: int, title: str, subtitle: str,
                title_color: str, stroke_color: str, subtitle_color: str,
                accent_color: str, style: str = "mosaic"):
    """Transparent PNG title card via Pillow. The base look (mosaic/epic) is a
    letterspaced stroked title with a soft drop shadow and the subtitle on a
    rounded dark "pill"; ``cascade`` swaps the pill for a full-width dark band
    with accent hairlines enclosing ALL the text; ``prism`` goes typographic —
    bigger tracked title without a stroke, uppercase wide-tracked subtitle, thin
    accent rules, all on a lighter full-width band the compose pass frosts (the
    kaleidoscope stays visible through the glass). Title font auto-shrinks to
    fit in every style.

    Returns None when there is nothing to draw (both texts empty), else
    ``{"glass": (x, y, w, h) | None}`` — the rect the compose pass should frost
    (the subtitle pill, cascade's band, or None when no frosting is wanted)."""
    if not title and not subtitle:
        return None
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    bold_path = _find_font(_FONTS_BOLD)
    reg_path = _find_font(_FONTS_REGULAR) or bold_path
    if not bold_path:
        raise RuntimeError("no TrueType font found for the intro title card")

    prism = style == "prism"
    if prism:
        subtitle = subtitle.upper()
    title_frac = 0.15 if prism else 0.125
    title_track = 0.16 if prism else 0.10
    sub_frac = 0.028 if prism else 0.034
    sub_track_frac = 0.38 if prism else 0.22
    cy_frac = 0.44 if prism else 0.46

    txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt)
    max_w = W * 0.88
    accent = _rgb(accent_color)
    top_y, bot_y = H, 0  # vertical extent of everything drawn (for band/rules)

    def tracked_width(text: str, font, tracking: float) -> float:
        return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)

    def draw_tracked(text: str, font, tracking: float, center_y: float, fill,
                     stroke_w: int = 0, stroke_fill=None) -> None:
        nonlocal top_y, bot_y
        ascent, descent = font.getmetrics()
        x = (W - tracked_width(text, font, tracking)) / 2
        y = center_y - (ascent + descent) / 2
        top_y = min(top_y, y)
        bot_y = max(bot_y, y + ascent + descent)
        for ch in text:
            draw.text((x, y), ch, font=font, fill=fill,
                      stroke_width=stroke_w, stroke_fill=stroke_fill)
            x += draw.textlength(ch, font=font) + tracking

    title_size = 0
    title_w = 0.0
    if title:
        title_size = max(18, round(H * title_frac))
        while title_size > 18:
            font = ImageFont.truetype(bold_path, title_size)
            if tracked_width(title, font, title_size * title_track) <= max_w:
                break
            title_size -= 4
        font = ImageFont.truetype(bold_path, title_size)
        title_w = tracked_width(title, font, title_size * title_track)
        stroke = 0 if prism else max(1, round(title_size * 0.025))
        cy = H * (cy_frac if subtitle else 0.5)
        draw_tracked(title, font, title_size * title_track, cy, _rgb(title_color),
                     stroke_w=stroke, stroke_fill=None if prism else _rgb(stroke_color))

    glass = None
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sub_w = 0.0
    if subtitle:
        sub_size = max(12, round(H * sub_frac))
        font = ImageFont.truetype(reg_path, sub_size)
        track = sub_size * sub_track_frac
        while sub_size > 12 and tracked_width(subtitle, font, track) > max_w:
            sub_size -= 2
            font = ImageFont.truetype(reg_path, sub_size)
            track = sub_size * sub_track_frac
        if prism:
            cy = H * cy_frac + (title_size * 0.95 if title else 0) + sub_size * 1.5
        else:
            cy = H * 0.46 + (title_size * 0.85 if title else 0) + sub_size
        sub_w = tracked_width(subtitle, font, track)
        if style in ("mosaic", "epic"):
            # Dark rounded pill behind the subtitle, with a whisper of the
            # subtitle color as its outline. Drawn on its own layer so the text
            # drop shadow (built from the TEXT alpha below) doesn't smear a
            # rectangle around it.
            ascent, descent = font.getmetrics()
            pad_x, pad_y = round(sub_size * 0.95), round(sub_size * 0.55)
            x0 = (W - sub_w) / 2 - pad_x
            y0 = cy - (ascent + descent) / 2 - pad_y
            x1 = (W + sub_w) / 2 + pad_x
            y1 = cy + (ascent + descent) / 2 + pad_y
            ImageDraw.Draw(deco).rounded_rectangle(
                (x0, y0, x1, y1), radius=(y1 - y0) / 2,
                fill=(0, 0, 0, 150), outline=(*_rgb(subtitle_color), 110),
                width=max(1, H // 480),
            )
            glass = (int(x0), int(y0), int(x1 - x0) + 1, int(y1 - y0) + 1)
        draw_tracked(subtitle, font, track, cy, _rgb(subtitle_color))

    if style == "cascade":
        # Full-width dark band enclosing every line of text, hairlined in the
        # accent color top and bottom. Part of the card so it rides the exact
        # same alpha fade; the compose pass frosts (blurs) the same rect, so the
        # scrolling wall shimmers through the glass.
        pad = round(H * 0.045)
        y0 = max(0, int(top_y) - pad)
        y1 = min(H, int(bot_y) + pad)
        band = ImageDraw.Draw(deco)
        band.rectangle((0, y0, W, y1), fill=(0, 0, 0, 118))
        lh = max(1, H // 420)
        band.rectangle((0, y0, W, y0 + lh), fill=(*accent, 135))
        band.rectangle((0, y1 - lh, W, y1), fill=(*accent, 135))
        glass = (0, y0, W, y1 - y0)
    elif prism:
        # Lighter full-width band (the kaleidoscope must stay visible through
        # the frost) with thin accent rules framing the text block — the
        # typographic detail that sells the style. Band drawn first, rules on top.
        pad = round(H * 0.075)
        y0 = max(0, int(top_y) - pad)
        y1 = min(H, int(bot_y) + pad)
        d = ImageDraw.Draw(deco)
        d.rectangle((0, y0, W, y1), fill=(0, 0, 0, 96))
        ref_w = title_w or sub_w or W * 0.5
        rw = min(max_w, ref_w * 0.85)
        rt = max(2, round(H / 380))
        gap = round(H * 0.045)
        d.rectangle(((W - rw) / 2, top_y - gap - rt, (W + rw) / 2, top_y - gap),
                    fill=(*accent, 205))
        d.rectangle(((W - rw) / 2, bot_y + gap, (W + rw) / 2, bot_y + gap + rt),
                    fill=(*accent, 205))
        glass = (0, y0, W, y1 - y0)

    # Soft drop shadow from the text's own alpha, composited underneath.
    silhouette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    silhouette.paste((0, 0, 0, 190), mask=txt.split()[3])
    shadow = silhouette.filter(ImageFilter.GaussianBlur(max(2, H // 200)))
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    card.alpha_composite(deco)
    card.alpha_composite(shadow, (0, max(2, H // 240)))
    card.alpha_composite(txt)
    card.save(path)
    return {"glass": glass}


def _glass_and_title(filters: list[str], prev: str, card, title_idx: int, *,
                     W: int, H: int, duration: float, drift: int = 0) -> str:
    """Shared tail of every style's graph: frost the card's glass rect (if any)
    and overlay the title card, both riding the same fade envelope. ``drift``
    (epic) slides the whole card up by that many px during the fade-in; the
    glass rect is extended downward to cover the travel."""
    if card is None:
        return prev
    t_in, t_in_d = 0.7, 1.3
    t_out, t_out_d = duration - 2.3, 1.4
    rect = card.get("glass")
    if rect:
        # Frosted glass: blur what the video actually shows in the rect and lay
        # it back in the same spot (the card's translucent fill then tints it
        # dark). Even-aligned and clamped to the canvas — yuva420p needs even
        # dims, and the alpha fade is what lets the blur melt in/out.
        px, py, pw, ph = rect
        ph += drift
        px, py = max(0, px - px % 2), max(0, py - py % 2)
        pw = min(W - px, pw + pw % 2)
        ph = min(H - py, ph + ph % 2)
        gblur = max(8, round(min(W, H) / 70))
        filters.append(f"[{prev}]split=2[glbase][glsrc]")
        filters.append(
            f"[glsrc]crop={pw}:{ph}:{px}:{py},boxblur={gblur}:2,format=yuva420p,"
            f"fade=t=in:st={t_in}:d={t_in_d}:alpha=1,"
            f"fade=t=out:st={t_out:.2f}:d={t_out_d}:alpha=1[glass]"
        )
        filters.append(f"[glbase][glass]overlay={px}:{py}[glassed]")
        prev = "glassed"
    filters.append(
        f"[{title_idx}:v]format=rgba,fade=t=in:st={t_in}:d={t_in_d}:alpha=1,"
        f"fade=t=out:st={t_out:.2f}:d={t_out_d}:alpha=1[title]"
    )
    if drift:
        y_expr = f"'{drift}*(1-min(max((t-{t_in})/{t_in_d},0),1))'"
        filters.append(f"[{prev}][title]overlay=0:{y_expr}[titled]")
    else:
        filters.append(f"[{prev}][title]overlay=0:0[titled]")
    return "titled"


def _finalize(inputs: list[str], filters: list[str], prev: str, *, duration: float,
              fps: str, audio: tuple[int, int] | None, video_args: list[str] | None,
              out_path: Path, grade: str) -> None:
    """Append the shared polish chain (grade/vignette/head+tail fades) and run
    the single compose encode, with the optional silent audio track matching the
    merge set so the lossless stream-copy concat survives."""
    filters.append(
        f"[{prev}]{grade},"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={duration - 1.1:.2f}:d=1.1,"
        f"fps={fps},format=yuv420p,setsar=1[outv]"
    )
    audio_index = inputs.count("-i")
    if audio:
        rate, channels = audio
        layout = "mono" if channels == 1 else "stereo"
        inputs += ["-f", "lavfi", "-t", f"{duration:.2f}",
                   "-i", f"anullsrc=channel_layout={layout}:sample_rate={rate}"]
    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", ";".join(filters), "-map", "[outv]"]
    if audio:
        cmd += ["-map", f"{audio_index}:a", "-c:a", "aac", "-b:a", "192k",
                "-ar", str(audio[0]), "-ac", str(audio[1])]
    cmd += [*(video_args or ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
                             "-pix_fmt", "yuv420p"]),
            "-t", f"{duration:.2f}", "-movflags", "+faststart", str(out_path)]
    _run(cmd, "intro compose")


# --- style: mosaic -----------------------------------------------------------

def _compose_mosaic(sources, out_path, workdir, *, W, H, fps, duration, colors,
                    card, title_png, audio, video_args, log):
    n = len(sources)
    cw, ch, border, outer, positions = _grid_layout(n, W, H)
    minis = [workdir / f"mini_{i:02d}.mp4" for i in range(n)]
    with ThreadPoolExecutor(max_workers=min(4, n)) as pool:
        futures = [
            pool.submit(_extract_mini, src, dst, cw, ch, border, outer,
                        colors["border"], fps)
            for src, dst in zip(sources, minis)
        ]
        for f in futures:
            f.result()

    bg_src = random.choice(sources)
    blur = max(6, round(min(W, H) / 90))
    inputs: list[str] = ["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", str(bg_src)]
    for m in minis:
        inputs += ["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", str(m)]
    if card:
        inputs += ["-loop", "1", "-t", f"{duration:.2f}", "-i", str(title_png)]

    stagger = min(0.35, max(0.1, (duration * 0.25 - 0.3) / max(1, n - 1)))
    filters = [
        f"[0:v]scale=iw*sar:ih,scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,fps={fps},"
        f"eq=brightness=-0.3:saturation=0.6:contrast=1.1,boxblur={blur}:1[bg]"
    ]
    for i in range(n):
        st = 0.3 + i * stagger
        filters.append(f"[{i + 1}:v]format=yuva420p,fade=t=in:st={st:.2f}:d=1.0:alpha=1[v{i + 1}]")
    prev = "bg"
    for i, (x, y) in enumerate(positions):
        out = f"tmp{i + 1}"
        filters.append(f"[{prev}][v{i + 1}]overlay={x}:{y}[{out}]")
        prev = out
    prev = _glass_and_title(filters, prev, card, 1 + n, W=W, H=H, duration=duration)
    _finalize(inputs, filters, prev, duration=duration, fps=fps, audio=audio,
              video_args=video_args, out_path=out_path,
              grade="eq=contrast=1.05:brightness=0.02:saturation=1.1,vignette=PI/4")


# --- style: epic -------------------------------------------------------------

def _extract_hero(src: Path, dst: Path, W: int, H: int, fps: str, fps_float: float,
                  seg: float, zoom_in: bool) -> None:
    """One full-canvas hero segment with a slow zoompan push baked in. The input
    is cover-scaled to 1.6x canvas first so zoompan's integer crop window steps
    stay sub-perceptual. Clips shorter than the segment get stretched into
    slow motion (epic anyway); pathologically short ones loop."""
    dur = _probe_duration(src)
    start = min(4.0, max(0.0, dur * 0.2))
    avail = max(0.0, dur - start - 0.05)
    pre: list[str] = []
    setpts = ""
    if avail >= seg:
        pass
    elif avail >= max(0.5, seg * 0.45):
        setpts = f"setpts=PTS/{avail / seg:.4f},"
    else:
        pre = ["-stream_loop", "-1"]
        start = 0.0
    up_w, up_h = _even(round(W * 1.6)), _even(round(H * 1.6))
    frames = max(1.0, seg * fps_float)
    if zoom_in:
        z = f"min(1.02+0.11*on/{frames:.1f},1.13)"
    else:
        z = f"max(1.13-0.11*on/{frames:.1f},1.02)"
    vf = (
        f"scale=iw*sar:ih,scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},setsar=1,{setpts}fps={fps},"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps},"
        f"format=yuv420p"
    )
    cmd = ["ffmpeg", "-y", *pre]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(src), "-t", f"{seg:.2f}", "-vf", vf, "-an",
            "-c:v", "libx264", "-crf", "19", "-preset", "veryfast", str(dst)]
    _run(cmd, f"intro hero {dst.name}", timeout=600)


def _compose_epic(sources, out_path, workdir, *, W, H, fps, duration, colors,
                  card, title_png, audio, video_args, log):
    fps_float = (lambda n, _, d: float(n) / float(d or 1))(*str(fps).partition("/"))
    n = max(1, min(3, int(duration // 4), len(sources)))
    overlap = 0.8
    seg = (duration + (n - 1) * overlap) / n
    heroes = [workdir / f"hero_{i:02d}.mp4" for i in range(n)]
    with ThreadPoolExecutor(max_workers=min(3, n)) as pool:
        futures = [
            pool.submit(_extract_hero, src, dst, W, H, fps, fps_float, seg, i % 2 == 0)
            for i, (src, dst) in enumerate(zip(sources, heroes))
        ]
        for f in futures:
            f.result()

    inputs: list[str] = []
    for h in heroes:
        inputs += ["-i", str(h)]
    if card:
        inputs += ["-loop", "1", "-t", f"{duration:.2f}", "-i", str(title_png)]

    filters = [f"[{i}:v]settb=AVTB,setsar=1[h{i}]" for i in range(n)]
    prev = "h0"
    for i in range(1, n):
        out = f"x{i}"
        offset = i * (seg - overlap)
        filters.append(
            f"[{prev}][h{i}]xfade=transition=fade:duration={overlap}:offset={offset:.3f}[{out}]"
        )
        prev = out

    # Cinematic letterbox: solid bars plus an accent hairline at each inner edge.
    bar = round(H * (0.11 if W > H else 0.085))
    hl = max(2, round(min(W, H) / 450))
    filters.append(
        f"[{prev}]drawbox=x=0:y=0:w={W}:h={bar}:color=black:t=fill,"
        f"drawbox=x=0:y={H - bar}:w={W}:h={bar}:color=black:t=fill,"
        f"drawbox=x=0:y={bar - hl}:w={W}:h={hl}:color=0x{colors['border']}@0.55:t=fill,"
        f"drawbox=x=0:y={H - bar}:w={W}:h={hl}:color=0x{colors['border']}@0.55:t=fill[barred]"
    )
    prev = "barred"
    prev = _glass_and_title(filters, prev, card, n, W=W, H=H, duration=duration,
                            drift=round(H * 0.02))
    _finalize(inputs, filters, prev, duration=duration, fps=fps, audio=audio,
              video_args=video_args, out_path=out_path,
              grade="eq=contrast=1.07:brightness=-0.01:saturation=1.05,vignette=PI/3.8")


# --- style: cascade ----------------------------------------------------------

def _build_column(minis: list[Path], tiles_idx: list[int], dst: Path, *, col_w: int,
                  tile_w: int, tile_h: int, row_gap: int, duration: float, fps: str) -> None:
    """One column of the wall: the assigned minis looped to full intro length,
    each padded with the wall-background gap, vstacked into a single tall strip
    the compose pass scrolls with a plain overlay expression."""
    inputs: list[str] = []
    for idx in tiles_idx:
        inputs += ["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", str(minis[idx])]
    k = len(tiles_idx)
    pad_x = (col_w - tile_w) // 2
    filters = [
        f"[{j}:v]pad={col_w}:{tile_h + row_gap}:{pad_x}:0:color=0x{_WALL_BG},setsar=1[t{j}]"
        for j in range(k)
    ]
    filters.append("".join(f"[t{j}]" for j in range(k)) + f"vstack=inputs={k}[v]")
    _run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[v]",
         "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", str(dst)],
        f"intro column {dst.name}", timeout=600,
    )


def _compose_cascade(sources, out_path, workdir, *, W, H, fps, duration, colors,
                     card, title_png, audio, video_args, log):
    n = len(sources)
    cols = 4 if W > H else 3
    unit = min(W, H)
    margin = round(W * 0.04)
    gap = max(8, round(unit * 0.03))
    border = max(2, round(unit * 0.005))
    outer = max(1, round(border / 2))
    frame = border + outer
    col_w = _even((W - 2 * margin - (cols - 1) * gap) // cols)
    cw = _even(col_w - 2 * frame)
    ch = _even(round(cw * H / W))
    tile_w, tile_h = cw + 2 * frame, ch + 2 * frame
    row_gap = gap + (tile_h + gap) % 2  # keep each stacked unit even for yuv420p
    total_w = cols * col_w + (cols - 1) * gap
    xs = [(W - total_w) // 2 + c * (col_w + gap) for c in range(cols)]

    # Parallax: per-column travel (fraction of canvas height over the full
    # duration) and alternating direction.
    travel_frac = [0.34, 0.55, 0.42, 0.62]
    travels = [round(H * travel_frac[c % 4]) for c in range(cols)]

    minis = [workdir / f"tile_{i:02d}.mp4" for i in range(n)]
    with ThreadPoolExecutor(max_workers=min(4, n)) as pool:
        futures = [
            pool.submit(_extract_mini, src, dst, cw, ch, border, outer,
                        colors["border"], fps)
            for src, dst in zip(sources, minis)
        ]
        for f in futures:
            f.result()

    columns = [workdir / f"col_{c:02d}.mp4" for c in range(cols)]
    col_tiles = []
    for c in range(cols):
        k = math.ceil((H + travels[c] + row_gap) / (tile_h + row_gap)) + 1
        col_tiles.append([(c * 2 + j) % n for j in range(k)])
    with ThreadPoolExecutor(max_workers=min(4, cols)) as pool:
        futures = [
            pool.submit(_build_column, minis, col_tiles[c], columns[c], col_w=col_w,
                        tile_w=tile_w, tile_h=tile_h, row_gap=row_gap,
                        duration=duration, fps=fps)
            for c in range(cols)
        ]
        for f in futures:
            f.result()

    inputs: list[str] = []
    for col in columns:
        inputs += ["-i", str(col)]
    if card:
        inputs += ["-loop", "1", "-t", f"{duration:.2f}", "-i", str(title_png)]

    filters = [f"color=c=0x{_WALL_BG}:s={W}x{H}:r={fps}:d={duration:.2f}[base]"]
    for c in range(cols):
        st = 0.25 + c * 0.12
        filters.append(f"[{c}:v]format=yuva420p,fade=t=in:st={st:.2f}:d=0.9:alpha=1[c{c}]")
    prev = "base"
    for c in range(cols):
        speed = travels[c] / duration
        if c % 2 == 0:
            y_expr = f"'-{speed:.3f}*t'"
        else:
            y_expr = f"'-{travels[c]}+{speed:.3f}*t'"
        out = f"w{c}"
        filters.append(f"[{prev}][c{c}]overlay={xs[c]}:{y_expr}[{out}]")
        prev = out
    filters.append(f"[{prev}]eq=brightness=-0.14:saturation=0.8[wall]")
    prev = "wall"
    prev = _glass_and_title(filters, prev, card, cols, W=W, H=H, duration=duration)
    _finalize(inputs, filters, prev, duration=duration, fps=fps, audio=audio,
              video_args=video_args, out_path=out_path,
              grade="eq=contrast=1.06:brightness=0.01:saturation=1.05,vignette=PI/3.6")


# --- style: prism ------------------------------------------------------------

def _extract_prism_panel(src: Path, dst: Path, W: int, H: int, fps: str,
                         fps_float: float, seg: float, zoom_in: bool,
                         spin: float) -> None:
    """One kaleidoscope panel: the clip cover-cropped to the full canvas becomes
    a quadrant of a 2Wx2H 4-way mirror (hflip/vflip stacks — the seams are
    continuous by construction, meeting in a mandala at the center), which then
    slowly rotates and zoompans back down to canvas size. Base zoom 2.0 shows
    exactly the seam cross; the 2x oversize doubles as zoompan's anti-jitter
    headroom AND rotation margin (the visible window never leaves the source).
    Short clips stretch into slow motion; pathologically short ones loop."""
    dur = _probe_duration(src)
    start = min(4.0, max(0.0, dur * 0.2))
    avail = max(0.0, dur - start - 0.05)
    pre: list[str] = []
    setpts = ""
    if avail >= seg:
        pass
    elif avail >= max(0.5, seg * 0.45):
        setpts = f"setpts=PTS/{avail / seg:.4f},"
    else:
        pre = ["-stream_loop", "-1"]
        start = 0.0
    frames = max(1.0, seg * fps_float)
    if zoom_in:
        z = f"min(2.0+0.26*on/{frames:.1f},2.26)"
    else:
        z = f"max(2.26-0.26*on/{frames:.1f},2.0)"
    fc = (
        f"[0:v]scale=iw*sar:ih,scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,{setpts}fps={fps},split=2[qa][qb];"
        f"[qb]hflip[qh];[qa][qh]hstack=inputs=2[top];"
        f"[top]split=2[ta][tb];[tb]vflip[bot];[ta][bot]vstack=inputs=2[mir];"
        f"[mir]rotate=a='{spin:.4f}*t':c=black,"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps},"
        f"format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", *pre]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(src), "-t", f"{seg:.2f}", "-filter_complex", fc, "-map", "[v]",
            "-an", "-c:v", "libx264", "-crf", "19", "-preset", "veryfast", str(dst)]
    _run(cmd, f"intro prism {dst.name}", timeout=600)


def _compose_prism(sources, out_path, workdir, *, W, H, fps, duration, colors,
                   card, title_png, audio, video_args, log):
    fps_float = (lambda n, _, d: float(n) / float(d or 1))(*str(fps).partition("/"))
    n = 2 if duration >= 10 and len(sources) >= 2 else 1
    overlap = 0.8
    seg = (duration + (n - 1) * overlap) / n
    panels = [workdir / f"prism_{i:02d}.mp4" for i in range(n)]
    with ThreadPoolExecutor(max_workers=max(1, n)) as pool:
        futures = [
            # Alternate zoom direction AND spin direction between the panels.
            pool.submit(_extract_prism_panel, src, dst, W, H, fps, fps_float, seg,
                        i % 2 == 0, 0.035 if i % 2 == 0 else -0.035)
            for i, (src, dst) in enumerate(zip(sources, panels))
        ]
        for f in futures:
            f.result()

    inputs: list[str] = []
    for p in panels:
        inputs += ["-i", str(p)]
    if card:
        inputs += ["-loop", "1", "-t", f"{duration:.2f}", "-i", str(title_png)]

    filters = [f"[{i}:v]settb=AVTB,setsar=1[p{i}]" for i in range(n)]
    prev = "p0"
    for i in range(1, n):
        out = f"x{i}"
        filters.append(
            f"[{prev}][p{i}]xfade=transition=fade:duration={overlap}:offset={i * (seg - overlap):.3f}[{out}]"
        )
        prev = out
    prev = _glass_and_title(filters, prev, card, n, W=W, H=H, duration=duration)
    _finalize(inputs, filters, prev, duration=duration, fps=fps, audio=audio,
              video_args=video_args, out_path=out_path,
              grade="eq=contrast=1.1:saturation=1.22,vignette=PI/3.6")


_COMPOSERS = {
    "mosaic": _compose_mosaic,
    "epic": _compose_epic,
    "cascade": _compose_cascade,
    "prism": _compose_prism,
}


def generate_intro(
    sources: list[Path],
    out_path: Path,
    workdir: Path,
    *,
    canvas: tuple[int, int],
    fps: str = "24",
    audio: tuple[int, int] | None = None,
    video_args: list[str] | None = None,
    options: dict | None = None,
    log=lambda msg: None,
) -> Path:
    """Render the intro to ``out_path``. ``sources`` (<= 9, already sampled by the
    caller) feed the chosen style (mosaic uses all as grid tiles, epic the first
    <= 3 as full-bleed heroes, cascade all as wall tiles, prism the first <= 2
    as kaleidoscope panels). ``canvas``/``fps`` should match the merge target so
    a uniform clip
    set keeps its lossless concat; ``audio`` = (sample_rate, channels) adds a
    matching silent AAC track for the same reason (None = no audio stream).
    ``video_args`` are the encoder args for the final compose (defaults to
    libx264 CRF 16)."""
    opts = {**DEFAULTS, **(options or {})}
    W, H = canvas
    if not sources:
        raise RuntimeError("intro: no source clips")
    if not re.fullmatch(r"[1-9]\d*(/[1-9]\d*)?", str(fps)):
        fps = "24"
    duration = max(6.0, min(20.0, float(opts.get("duration") or 12.0)))
    style = str(opts.get("style") or "mosaic").lower()
    if style not in STYLES:
        style = "mosaic"
    colors = {
        "title": _hex6(opts.get("title_color"), DEFAULTS["title_color"]),
        "stroke": _hex6(opts.get("stroke_color"), DEFAULTS["stroke_color"]),
        "subtitle": _hex6(opts.get("subtitle_color"), DEFAULTS["subtitle_color"]),
        "border": _hex6(opts.get("border_color"), DEFAULTS["border_color"]),
    }

    workdir.mkdir(parents=True, exist_ok=True)
    log(f"intro: style={style}, {len(sources)} clip(s) on {W}x{H} @ {fps}fps, {duration:.0f}s")

    title_png = workdir / "title.png"
    card = _title_card(title_png, W, H, str(opts.get("title") or "").strip(),
                       str(opts.get("subtitle") or "").strip(),
                       colors["title"], colors["stroke"], colors["subtitle"],
                       colors["border"], style)

    _COMPOSERS[style](
        sources, out_path, workdir, W=W, H=H, fps=fps, duration=duration,
        colors=colors, card=card, title_png=title_png, audio=audio,
        video_args=video_args, log=log,
    )
    log("intro: composed")
    return out_path


if __name__ == "__main__":  # dev-only smoke CLI: python introgen.py out.mp4 clip1 [clip2 ...]
    import argparse

    ap = argparse.ArgumentParser(description="Render a cinematic intro from sample clips")
    ap.add_argument("output")
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--style", default="mosaic", choices=STYLES)
    ap.add_argument("--title", default="THE NIGHT RIDER")
    ap.add_argument("--subtitle", default="Scenes of Awesome")
    ap.add_argument("--canvas", default="1280x720")
    ap.add_argument("--fps", default="24")
    ap.add_argument("--duration", type=float, default=12.0)
    args = ap.parse_args()
    w, h = (int(v) for v in args.canvas.split("x"))
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="intro_"))
    generate_intro(
        [Path(v) for v in args.videos][:9], Path(args.output), work,
        canvas=(w, h), fps=args.fps, audio=(48000, 2),
        options={"title": args.title, "subtitle": args.subtitle,
                 "duration": args.duration, "style": args.style},
        log=print,
    )
    print(f"done -> {args.output}")
