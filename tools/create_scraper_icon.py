"""Create a multi-resolution Windows icon using the installed Pillow library."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SIZE = 768
SCALE = SIZE / 512


def box(points):
    return tuple(round(value * SCALE) for value in points)


def create():
    """Render a book, headphones and update badge on a dark rounded tile."""
    folder = ROOT / 'assets'
    folder.mkdir(exist_ok=True)
    mask = Image.new('L', (SIZE, SIZE))
    ImageDraw.Draw(mask).rounded_rectangle(box((20, 20, 492, 492)), radius=round(108 * SCALE), fill=255)
    image = Image.new('RGBA', (SIZE, SIZE))
    draw = ImageDraw.Draw(image)
    for y in range(SIZE):
        ratio = y / SIZE
        draw.line((0, y, SIZE, y), fill=(round(29 - 16 * ratio), round(46 - 18 * ratio), round(78 - 30 * ratio), 255))
    image.putalpha(mask)
    glow = Image.new('RGBA', image.size)
    gd = ImageDraw.Draw(glow)
    gd.ellipse(box((60, 25, 410, 340)), fill=(34, 211, 198, 35))
    glow = glow.filter(ImageFilter.GaussianBlur(48 * SCALE))
    glow.putalpha(Image.composite(glow.getchannel('A'), Image.new('L', image.size), mask))
    image = Image.alpha_composite(image, glow)
    draw = ImageDraw.Draw(image)
    # Słuchawki nad otwartą książką; szerokie kształty pozostają czytelne na pulpicie.
    draw.arc(box((115, 100, 397, 382)), 180, 360, fill='#E6F9FF', width=round(24 * SCALE))
    for bounds in ((102, 218, 143, 305), (369, 218, 410, 305)):
        draw.rounded_rectangle(box(bounds), radius=round(17 * SCALE), fill='#E6F9FF')
    left = [(147, 251), (189, 242), (256, 264), (256, 383), (188, 361), (147, 370)]
    right = [(256, 264), (323, 242), (365, 251), (365, 370), (324, 361), (256, 383)]
    draw.polygon([box(p) for p in left], fill='#44D9CA')
    draw.polygon([box(p) for p in right], fill='#81F0DC')
    draw.line([box((256, 269)), box((256, 380))], fill='#17384D', width=round(7 * SCALE))
    for y in (283, 307, 331):
        draw.line([box((170, y)), box((226, y + 12))], fill='#1F9A98', width=round(5 * SCALE))
    # Osobny znacznik aktualizacji, bez nazwy ani inicjałów innego projektu.
    draw.ellipse(box((339, 334, 465, 460)), fill='#F6B950', outline='#15283E', width=round(8 * SCALE))
    draw.arc(box((370, 365, 434, 429)), 35, 305, fill='#203044', width=round(10 * SCALE))
    draw.polygon([box(p) for p in ((420, 360), (440, 375), (416, 383))], fill='#203044')
    image.save(folder / 'scrapery-audiobookow.png')
    image.save(folder / 'scrapery-audiobookow.ico', sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    return folder / 'scrapery-audiobookow.ico'


if __name__ == '__main__':
    print(create())
