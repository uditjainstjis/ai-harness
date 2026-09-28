"""Draws the Pramana app icon (a "P" carrying a proof tick) and writes Pramana.icns. Needs Pillow; the .icns is committed."""
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
top, bottom = (79, 70, 229), (124, 58, 237)                  # indigo -> violet
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([100, 100, S - 100, S - 100], radius=190, fill=255)   # macOS icon grid margin
img.paste(grad, (0, 0), mask)
font = None
for f in ("/System/Library/Fonts/SFNSRounded.ttf", "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc"):
    if os.path.exists(f):
        font = ImageFont.truetype(f, 560)
        break
d.text((S // 2 - 40, S // 2 + 10), "P", font=font, fill=(255, 255, 255, 255), anchor="mm")
# the proof tick, bottom right, in a small white disc
cx, cy, r = 700, 700, 120
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
d.line([(cx - 55, cy + 2), (cx - 12, cy + 45), (cx + 60, cy - 45)], fill=(22, 163, 74, 255), width=34, joint="curve")

tmp = tempfile.mkdtemp()
iconset = os.path.join(tmp, "Pramana.iconset")
os.makedirs(iconset)
for size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        px = size * scale
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
        img.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))
img.save(os.path.join(os.path.dirname(__file__), "icon_1024.png"))
subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(os.path.dirname(os.path.abspath(__file__)), "Pramana.icns")], check=True)
shutil.rmtree(tmp)
print("wrote Pramana.icns")
