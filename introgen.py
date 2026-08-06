"""Cinematic intro generator for the MP4 merge export.

Builds a trailer-style opening (adapted from specs/introgen.py) that the export
route prepends to a merge: a darkened/blurred background taken from one of the
clips, a bordered grid of up to 9 mini-clips fading in staggered, and a title +
subtitle card that fades in and out, finished with a grade, vignette and fades.

Deliberate departures from the spec script it grew from:
- No ImageMagick. The title card is drawn with Pillow (already a hard dep) —
  which also sidesteps ffmpeg drawtext's escaping pitfalls for user-typed text.
- The canvas is the MERGE TARGET (size/fps decided by the caller from the real
  clip set), not a hardcoded 1280x720 — portrait and square merges get portrait
  and square intros, and a uniform clip set keeps its lossless concat because
  the intro is encoded to the exact same signature (incl. a matching silent
  audio track when the clips carry audio).
- The spec's extract -> loop -> border -> compose -> polish chain (5 encodes per
  mini) collapses into one small encode per mini (extract+cover-crop+border) and
  ONE full-res compose pass (loops via -stream_loop, fades/overlays/title/grade/
  vignette in a single filter graph).
"""

from __future__ import annotations

import math
import random
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULTS = {
    "title": "",
    "subtitle": "",
    "title_color": "#D4AF37",
    "stroke_color": "#8B6914",
    "subtitle_color": "#E8D5A3",
    "border_color": "#D4AF37",
    "duration": 12.0,
}

# Outer dark frame around each tile's colored border (spec's #1a1a1a).
_OUTER_COLOR = "1a1a1a"

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
                title_color: str, stroke_color: str, subtitle_color: str):
    """Transparent PNG title card via Pillow: letterspaced title with a stroke and
    a soft drop shadow, subtitle below on a rounded dark "pill" (the tile grid
    behind the card is bright, so bare subtitle text washes out — the pill plus
    the compose pass's backdrop blur of the same region keeps it readable on any
    footage). The title font auto-shrinks to fit.

    Returns None when there is nothing to draw (both texts empty), else
    ``{"pill": (x, y, w, h) | None}`` — the pill's canvas rect, for the compose
    pass to frost (None when there's no subtitle)."""
    if not title and not subtitle:
        return None
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    bold_path = _find_font(_FONTS_BOLD)
    reg_path = _find_font(_FONTS_REGULAR) or bold_path
    if not bold_path:
        raise RuntimeError("no TrueType font found for the intro title card")

    txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt)
    max_w = W * 0.88

    def tracked_width(text: str, font, tracking: float) -> float:
        return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)

    def draw_tracked(text: str, font, tracking: float, center_y: float, fill,
                     stroke_w: int = 0, stroke_fill=None) -> None:
        ascent, descent = font.getmetrics()
        x = (W - tracked_width(text, font, tracking)) / 2
        y = center_y - (ascent + descent) / 2
        for ch in text:
            draw.text((x, y), ch, font=font, fill=fill,
                      stroke_width=stroke_w, stroke_fill=stroke_fill)
            x += draw.textlength(ch, font=font) + tracking

    title_size = 0
    if title:
        title_size = max(18, round(H * 0.125))
        while title_size > 18:
            font = ImageFont.truetype(bold_path, title_size)
            if tracked_width(title, font, title_size * 0.10) <= max_w:
                break
            title_size -= 4
        font = ImageFont.truetype(bold_path, title_size)
        stroke = max(1, round(title_size * 0.025))
        cy = H * (0.46 if subtitle else 0.5)
        draw_tracked(title, font, title_size * 0.10, cy, _rgb(title_color),
                     stroke_w=stroke, stroke_fill=_rgb(stroke_color))

    pill = None
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if subtitle:
        sub_size = max(12, round(H * 0.034))
        font = ImageFont.truetype(reg_path, sub_size)
        track = sub_size * 0.22
        while sub_size > 12 and tracked_width(subtitle, font, track) > max_w:
            sub_size -= 2
            font = ImageFont.truetype(reg_path, sub_size)
            track = sub_size * 0.22
        cy = H * 0.46 + (title_size * 0.85 if title else 0) + sub_size
        # Dark rounded pill behind the subtitle, with a whisper of the subtitle
        # color as its outline. Drawn on its own layer so the text drop shadow
        # (built from the TEXT alpha below) doesn't smear a rectangle around it.
        sw = tracked_width(subtitle, font, track)
        ascent, descent = font.getmetrics()
        pad_x, pad_y = round(sub_size * 0.95), round(sub_size * 0.55)
        x0 = (W - sw) / 2 - pad_x
        y0 = cy - (ascent + descent) / 2 - pad_y
        x1 = (W + sw) / 2 + pad_x
        y1 = cy + (ascent + descent) / 2 + pad_y
        ImageDraw.Draw(deco).rounded_rectangle(
            (x0, y0, x1, y1), radius=(y1 - y0) / 2,
            fill=(0, 0, 0, 150), outline=(*_rgb(subtitle_color), 110),
            width=max(1, H // 480),
        )
        pill = (int(x0), int(y0), int(x1 - x0) + 1, int(y1 - y0) + 1)
        draw_tracked(subtitle, font, track, cy, _rgb(subtitle_color))

    # Soft drop shadow from the text's own alpha, composited underneath.
    silhouette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    silhouette.paste((0, 0, 0, 190), mask=txt.split()[3])
    shadow = silhouette.filter(ImageFilter.GaussianBlur(max(2, H // 200)))
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    card.alpha_composite(deco)
    card.alpha_composite(shadow, (0, max(2, H // 240)))
    card.alpha_composite(txt)
    card.save(path)
    return {"pill": pill}


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
    caller) fill the grid; the background comes from a random one of them.
    ``canvas``/``fps`` should match the merge target so a uniform clip set keeps
    its lossless concat; ``audio`` = (sample_rate, channels) adds a matching
    silent AAC track for the same reason (None = no audio stream). ``video_args``
    are the encoder args for the final compose (defaults to libx264 CRF 16)."""
    opts = {**DEFAULTS, **(options or {})}
    W, H = canvas
    if not sources:
        raise RuntimeError("intro: no source clips")
    if not re.fullmatch(r"[1-9]\d*(/[1-9]\d*)?", str(fps)):
        fps = "24"
    duration = max(6.0, min(20.0, float(opts.get("duration") or 12.0)))
    title_color = _hex6(opts.get("title_color"), DEFAULTS["title_color"])
    stroke_color = _hex6(opts.get("stroke_color"), DEFAULTS["stroke_color"])
    subtitle_color = _hex6(opts.get("subtitle_color"), DEFAULTS["subtitle_color"])
    border_color = _hex6(opts.get("border_color"), DEFAULTS["border_color"])

    workdir.mkdir(parents=True, exist_ok=True)
    n = len(sources)
    cw, ch, border, outer, positions = _grid_layout(n, W, H)
    log(f"intro: {n} tile(s) on {W}x{H} @ {fps}fps, {duration:.0f}s")

    minis = [workdir / f"mini_{i:02d}.mp4" for i in range(n)]
    with ThreadPoolExecutor(max_workers=min(4, n)) as pool:
        futures = [
            pool.submit(_extract_mini, src, dst, cw, ch, border, outer, border_color, fps)
            for src, dst in zip(sources, minis)
        ]
        for f in futures:
            f.result()

    title_png = workdir / "title.png"
    card = _title_card(title_png, W, H, str(opts.get("title") or "").strip(),
                       str(opts.get("subtitle") or "").strip(),
                       title_color, stroke_color, subtitle_color)
    has_title = card is not None

    # --- single compose pass -------------------------------------------------
    bg_src = random.choice(sources)
    blur = max(6, round(min(W, H) / 90))
    inputs: list[str] = ["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", str(bg_src)]
    for m in minis:
        inputs += ["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", str(m)]
    if has_title:
        inputs += ["-loop", "1", "-t", f"{duration:.2f}", "-i", str(title_png)]
    audio_index = 1 + n + (1 if has_title else 0)
    if audio:
        rate, channels = audio
        layout = "mono" if channels == 1 else "stereo"
        inputs += ["-f", "lavfi", "-t", f"{duration:.2f}",
                   "-i", f"anullsrc=channel_layout={layout}:sample_rate={rate}"]

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
    if has_title:
        # Title/subtitle fade timings — the frosted backdrop below must ride the
        # exact same envelope so the glass appears and vanishes with the text.
        t_in, t_in_d = 0.7, 1.3
        t_out, t_out_d = duration - 2.3, 1.4
        pill = card.get("pill")
        if pill:
            # Frosted glass behind the subtitle: blur what the video actually
            # shows in the pill's rect and lay it back in the same spot (the
            # card's translucent pill then tints it dark). Even-aligned and
            # clamped to the canvas — yuva420p needs even dims, and the alpha
            # fade is what lets the blur melt in/out instead of popping.
            px, py, pw, ph = pill
            px, py = max(0, px - px % 2), max(0, py - py % 2)
            pw = min(W - px, pw + pw % 2)
            ph = min(H - py, ph + ph % 2)
            blur = max(8, round(min(W, H) / 70))
            filters.append(f"[{prev}]split=2[glbase][glsrc]")
            filters.append(
                f"[glsrc]crop={pw}:{ph}:{px}:{py},boxblur={blur}:2,format=yuva420p,"
                f"fade=t=in:st={t_in}:d={t_in_d}:alpha=1,"
                f"fade=t=out:st={t_out:.2f}:d={t_out_d}:alpha=1[glass]"
            )
            filters.append(f"[glbase][glass]overlay={px}:{py}[glassed]")
            prev = "glassed"
        filters.append(
            f"[{1 + n}:v]format=rgba,fade=t=in:st={t_in}:d={t_in_d}:alpha=1,"
            f"fade=t=out:st={t_out:.2f}:d={t_out_d}:alpha=1[title]"
        )
        filters.append(f"[{prev}][title]overlay=0:0[titled]")
        prev = "titled"
    filters.append(
        f"[{prev}]eq=contrast=1.05:brightness=0.02:saturation=1.1,vignette=PI/4,"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={duration - 1.1:.2f}:d=1.1,"
        f"fps={fps},format=yuv420p,setsar=1[outv]"
    )

    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", ";".join(filters), "-map", "[outv]"]
    if audio:
        cmd += ["-map", f"{audio_index}:a", "-c:a", "aac", "-b:a", "192k",
                "-ar", str(audio[0]), "-ac", str(audio[1])]
    cmd += [*(video_args or ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
                             "-pix_fmt", "yuv420p"]),
            "-t", f"{duration:.2f}", "-movflags", "+faststart", str(out_path)]
    _run(cmd, "intro compose")
    log("intro: composed")
    return out_path


if __name__ == "__main__":  # dev-only smoke CLI: python introgen.py out.mp4 clip1 [clip2 ...]
    import argparse

    ap = argparse.ArgumentParser(description="Render a cinematic intro from sample clips")
    ap.add_argument("output")
    ap.add_argument("videos", nargs="+")
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
        options={"title": args.title, "subtitle": args.subtitle, "duration": args.duration},
        log=print,
    )
    print(f"done -> {args.output}")
