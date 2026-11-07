from __future__ import annotations
import html
import io
import json
import math
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from PIL import Image, ImageOps
from .adapters import HTTP


def download_images(items, output, workers=4):
    folder = output / 'pictures'
    folder.mkdir()
    errors = []
    def download(p):
        if not p.get('image_source'):
            return p['id'], 'No image URL supplied'
        try:
            raw = HTTP(delay=0.1).get(p['image_source'], binary=True)
            with Image.open(io.BytesIO(raw)) as src:
                if src.width * src.height > 40_000_000:
                    raise ValueError('Image exceeds 40 megapixels')
                image = ImageOps.exif_transpose(src).convert('RGB')
                image.thumbnail((1000, 1400))
                target = folder / (p['id'] + '.jpg')
                image.save(target, 'JPEG', quality=83, optimize=True)
            p['image'] = 'pictures/' + target.name
            return p['id'], None
        except Exception as e:
            return p['id'], str(e)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(download, p) for p in items]):
            identity, error = future.result()
            if error:
                errors.append({'id': identity, 'error': error})
    return errors


def write_html(items, report, output):
    template = Path(__file__).with_name('template.html').read_text(encoding='utf-8')
    payload = json.dumps({'products': items, 'report': report}, ensure_ascii=False).replace('<', chr(92) + 'u003c')
    page = template.replace('__TITLE__', html.escape(report['title'])).replace('__DATA__', payload)
    (output / 'index.html').write_text(page, encoding='utf-8')


def make_pdf(items, report, target, font_path=None):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.colors import HexColor
    candidates = [font_path] if font_path else [
        'C:/Windows/Fonts/simhei.ttf', '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    font = 'Helvetica'
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            pdfmetrics.registerFont(TTFont('CatalogFont', candidate))
            font = 'CatalogFont'
            break
    if font_path and font == 'Helvetica':
        raise ValueError('Configured PDF font does not exist')
    c = canvas.Canvas(str(target), pagesize=(595, 842), pageCompression=1)
    c.setTitle(report['title'])
    def text(x, y, value, size=10):
        c.setFillColor(HexColor('#302a25')); c.setFont(font, size)
        c.drawString(x, y, str(value))
    def lines(value, width, size=10):
        result, line = [], ''
        # Character wrapping works for English and CJK titles and avoids unbroken long strings.
        for ch in str(value):
            if pdfmetrics.stringWidth(line + ch, font, size) > width and line:
                result.append(line); line = ch
            else:
                line += ch
        if line: result.append(line)
        return result
    text(28, 780, 'OFFLINE SHOP CATALOG', 22)
    for i, line in enumerate(lines(report['title'], 530, 16)):
        text(28, 735 - i * 22, line, 16)
    notes = [f"{len(items)} products / {report['fetched_at']}",
             'Images and prices are saved locally. No internet required to browse.',
             'Currency is shown per product. No exchange-rate conversion is applied.',
             'Prices and stock are snapshots. Shipping and taxes may be excluded.',
             'This catalog covers the requested sources and configured limits.',
             'Clicking a product opens its website and requires internet access.',
             'Use the HTML version for search, filters, sorting, and favorites.',
             f"Missing images: {len(report['image_errors'])}. Source errors: {len(report['errors'])}.",
             'See report.json for limits, source URLs, warnings, and capture details.']
    y = 650
    for note in notes:
        for line in lines(note, 535, 11): text(28, y, line, 11); y -= 17
        y -= 12
    c.showPage()
    pages = math.ceil(len(items) / 4)
    for page, start in enumerate(range(0, len(items), 4), 1):
        if start % 100 == 0:
            key = 'p' + str(start); c.bookmarkPage(key)
            c.addOutlineEntry(f'Products {start+1}-{min(start+100,len(items))}', key, 0)
        text(24, 810, 'OFFLINE CATALOG', 13)
        text(405, 810, report['fetched_at'][:10], 10)
        for pos, p in enumerate(items[start:start+4]):
            x, y = 24 + (pos % 2) * 282, 458 - (pos // 2) * 380
            w, h = 265, 315
            c.setFillColor(HexColor('#f3f0ed')); c.rect(x, y, w, h, fill=1, stroke=0)
            if p.get('image'):
                with Image.open(target.parent / p['image']) as image:
                    iw, ih = image.size; scale = min(w / iw, h / ih)
                    c.drawImage(ImageReader(image), x+(w-iw*scale)/2, y+(h-ih*scale)/2, iw*scale, ih*scale)
            else: text(x+65, y+150, 'Image unavailable')
            title_lines = lines(p['title'], w, 9)
            if len(title_lines) > 2:
                title_lines = title_lines[:2]
                title_lines[-1] = title_lines[-1][:-3] + '...'
            for j, line in enumerate(title_lines): text(x, y-13-j*12, line, 9)
            price = 'Price not provided' if p['price'] is None else f"{p['currency'] or 'Currency unknown'} {p['price']:,.2f}"
            if p['price'] is not None and p['max_price'] != p['price']:
                price += f" - {p['max_price']:,.2f}"
            text(x, y-40, price, 11)
            c.linkURL(p['url'], (x, y-44, x+w, y+h), relative=0, thickness=0)
        text(24, 18, 'Snapshot data / Full titles and sources in HTML and products.json', 8)
        text(510, 18, f'{page}/{pages}', 8)
        c.showPage()
    c.save()


def package(output):
    return shutil.make_archive(str(output.parent / (output.name + '-offline')), 'zip', output.parent, output.name)
