from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
import qrcode

WIDTH = 1200
FONT_CACHE_PATH = Path("data/plugin_data/astrbot_plugin_live_notice/fonts/LXGWWenKaiScreen-v1.530.ttf")
FONT_CANDIDATES = (
    str(FONT_CACHE_PATH),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _font(size: int, configured: str | None = None) -> ImageFont.FreeTypeFont:
    choices = ([configured] if configured else []) + list(FONT_CANDIDATES)
    for path in choices:
        try:
            if Path(path).is_file():
                return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    raise FileNotFoundError("No CJK font found")


def _open(payload: bytes | None) -> Image.Image | None:
    if not payload:
        return None
    try:
        return Image.open(BytesIO(payload)).convert("RGB")
    except Exception:
        return None


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, stroke_width: int = 0
) -> str:
    text = str(text or "")
    left, _, right, _ = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    if right - left <= width:
        return text
    while text:
        text = text[:-1]
        left, _, right, _ = draw.textbbox((0, 0), text + "…", font=font, stroke_width=stroke_width)
        if right - left <= width:
            break
    return text + "…"


def _draw_heading(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    width: int,
    color: str,
) -> None:
    stroke_width = 1
    fitted = _fit_text(draw, text, font, width, stroke_width=stroke_width)
    draw.text(position, fitted, font=font, fill=color, stroke_width=stroke_width)


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    background: str,
    color: str = "#ffffff",
) -> None:
    left, top, right, bottom = bounds
    ink_left, ink_top, ink_right, ink_bottom = draw.textbbox((0, 0), text, font=font)
    x = (left + right - ink_left - ink_right) // 2
    y = (top + bottom - ink_top - ink_bottom) // 2
    draw.rounded_rectangle(bounds, radius=(bottom - top) // 2, fill=background)
    draw.text((x, y), text, font=font, fill=color)


def _avatar_layer(avatar: Image.Image | None, size: int) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if avatar:
        avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        layer.paste(avatar, (0, 0), mask)
    else:
        d = ImageDraw.Draw(layer)
        d.ellipse((0, 0, size - 1, size - 1), fill=(210, 218, 228, 255))
        d.ellipse((size * .25, size * .14, size * .75, size * .64), fill=(244, 245, 248, 255))
        d.rounded_rectangle((size * .15, size * .61, size * .85, size * 1.15), radius=size * .3, fill=(244, 245, 248, 255))
    return layer


def _qr(url: str, size: int) -> Image.Image:
    code = qrcode.QRCode(box_size=7, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(url)
    code.make(fit=True)
    return code.make_image(fill_color="#161923", back_color="#ffffff").convert("RGB").resize((size, size), Image.Resampling.NEAREST)


def _rounded_cover(cover: Image.Image, size: tuple[int, int], radius: int) -> Image.Image:
    image = ImageOps.fit(cover, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)).convert("RGBA")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    image.putalpha(mask)
    return image


def _description_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    text = " ".join(str(text or "").split())
    if not text:
        return []
    lines: list[str] = []
    line = ""
    for char in text:
        candidate = line + char
        if line and draw.textbbox((0, 0), candidate, font=font)[2] > width:
            lines.append(line)
            line = char
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def render_notice_card(
    event_type: str,
    info: dict,
    avatar_bytes: bytes | None,
    cover_bytes: bytes,
    output_path: str,
    font_path: str | None = None,
) -> None:
    cover = _open(cover_bytes)
    if cover is None:
        raise ValueError("Invalid cover image")
    avatar = _open(avatar_bytes)
    if event_type == "live":
        _render_live(info, avatar, cover, output_path, font_path)
    else:
        _render_end(info, avatar, cover, output_path, font_path)


def _render_live(info: dict, avatar: Image.Image | None, cover: Image.Image, output_path: str, font_path: str | None) -> None:
    canvas = Image.new("RGB", (WIDTH, 1160), "#e9edf1")
    draw = ImageDraw.Draw(canvas)
    title_font, name_font, small_font = _font(44, font_path), _font(36, font_path), _font(25, font_path)
    _draw_heading(draw, (58, 48), info.get("title", "直播中"), title_font, 850, "#252a33")
    avatar_layer = _avatar_layer(avatar, 150)
    canvas.paste(avatar_layer, (58, 132), avatar_layer)
    draw = ImageDraw.Draw(canvas)
    _draw_heading(draw, (235, 157), info.get("name", "主播"), name_font, 675, "#303641")
    category = str(info.get("category") or "直播")
    draw.text((235, 211), _fit_text(draw, category, small_font, 710), font=small_font, fill="#b76a4f")
    qr_img = _qr(str(info.get("url", "")), 210)
    canvas.paste(qr_img, (932, 42))
    card = _rounded_cover(cover, (1120, 700), 30)
    canvas.paste(card, (40, 360), card)
    draw = ImageDraw.Draw(canvas)
    label = "● 正在直播"
    label_font = _font(28, font_path)
    box = draw.textbbox((0, 0), label, font=label_font)
    label_w = box[2] - box[0] + 46
    _draw_pill(draw, (WIDTH - label_w - 72, 395, WIDTH - 72, 458), label, label_font, "#212c3b")
    description_font = _font(25, font_path)
    lines = _description_lines(draw, info.get("description", ""), description_font, WIDTH - 120)
    if lines:
        height = max(1160, 1090 + len(lines) * 38 + 36)
        extended = Image.new("RGB", (WIDTH, height), "#e9edf1")
        extended.paste(canvas, (0, 0))
        canvas = extended
        draw = ImageDraw.Draw(canvas)
        for index, line in enumerate(lines):
            draw.text((60, 1090 + index * 38), line, font=description_font, fill="#343a43")
    canvas.save(output_path, "JPEG", quality=91, optimize=True)


def _render_end(info: dict, avatar: Image.Image | None, cover: Image.Image, output_path: str, font_path: str | None) -> None:
    height = 1260
    canvas = Image.new("RGB", (WIDTH, height), "#29293b")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((52, 46, WIDTH - 52, 340), radius=40, fill="#37364e")
    avatar_layer = _avatar_layer(avatar, 120)
    canvas.paste(avatar_layer, (92, 90), avatar_layer)
    draw = ImageDraw.Draw(canvas)
    name_font, title_font = _font(45, font_path), _font(41, font_path)
    badge_font, small_font = _font(25, font_path), _font(24, font_path)
    _draw_heading(draw, (238, 116), info.get("name", "主播"), name_font, 615, "#f4f3ff")
    _draw_pill(draw, (875, 105, 1090, 172), "直播已结束", badge_font, "#4a4964", "#f3f2ff")
    _draw_heading(draw, (92, 245), info.get("title", "直播回顾"), title_font, 1010, "#ffffff")
    frame = _rounded_cover(cover, (1056, 710), 28)
    canvas.paste(frame, (72, 384), frame)
    duration = int(info.get("duration") or 0)
    duration_text = f"直播时长：{duration // 3600} 小时 {(duration % 3600) // 60} 分钟" if duration else "本场直播已结束"
    box = draw.textbbox((0, 0), duration_text, font=small_font)
    badge_w = min(550, box[2] - box[0] + 44)
    badge_text = _fit_text(draw, duration_text, small_font, badge_w - 44)
    _draw_pill(draw, (WIDTH - badge_w - 96, 1014, WIDTH - 96, 1074), badge_text, small_font, "#262736")
    lines = _description_lines(draw, info.get("description", ""), small_font, WIDTH - 144)
    if lines:
        height = max(1260, 1122 + len(lines) * 36 + 42)
        extended = Image.new("RGB", (WIDTH, height), "#29293b")
        extended.paste(canvas, (0, 0))
        canvas = extended
        draw = ImageDraw.Draw(canvas)
        for index, line in enumerate(lines):
            draw.text((72, 1122 + index * 36), line, font=small_font, fill="#e5e3f0")
    canvas.save(output_path, "JPEG", quality=91, optimize=True)
