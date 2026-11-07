from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from .adapters import HTTP, State, extract, product, web_url
from .render import download_images, write_html, make_pdf, package


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build a local image-first shopping catalog. Never assumes every website is supported.')
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument('--url', help='Collection, product, or public listing URL')
    sources.add_argument('--config', type=Path, help='JSON configuration with sources[]')
    sources.add_argument('--input', type=Path, help='Import normalized product JSON instead of crawling')
    parser.add_argument('--adapter', choices=['auto', 'shopify', 'skims', 'generic', 'selectors'], default='auto')
    parser.add_argument('--output', type=Path, default=Path('output/catalog'))
    parser.add_argument('--title', default='My Offline Shop')
    parser.add_argument('--max-products', type=int, default=200)
    parser.add_argument('--max-pages', type=int, default=50, help='Global page budget across all sources')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--delay', type=float, default=0.4)
    parser.add_argument('--pdf', action='store_true')
    parser.add_argument('--font', help='Optional embedded TrueType font for PDF titles')
    args = parser.parse_args(argv)
    if not 1 <= args.max_products <= 10000 or not 1 <= args.max_pages <= 2000:
        parser.error('max-products must be 1..10000; max-pages must be 1..2000')
    if not 1 <= args.workers <= 8 or not 0 <= args.delay <= 60:
        parser.error('workers must be 1..8; delay must be 0..60 seconds')
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Output directory must be empty; choose a new folder to preserve existing catalogs')
    config = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
    source_list = config.get('sources', []) if args.config else ([{'url': args.url, 'adapter': args.adapter}] if args.url else [])
    for source in source_list:
        source['url'] = web_url(source['url'])
    state = State(max_pages=args.max_pages)
    report = {'title': config.get('title', args.title), 'fetched_at': datetime.now(timezone.utc).isoformat(),
              'limits': {'products': args.max_products, 'pages': args.max_pages},
              'sources': [], 'warnings': state.warnings, 'errors': [], 'image_errors': []}
    found = {}
    def add(p, name):
        key = (p['url'], p['currency'])
        if key in found:
            if name not in found[key]['collections']: found[key]['collections'].append(name)
            return
        p['collections'] = [name]
        found[key] = p
    if args.input:
        data = json.loads(args.input.read_text(encoding='utf-8-sig'))
        if not isinstance(data, list): parser.error('Import must be an array of normalized products')
        for row in data[:args.max_products]:
            p = product(row['url'], row['title'], row.get('image_source') or row.get('image'),
                        row.get('price'), row.get('max_price'), row.get('currency'), row.get('available'), row.get('type', ''))
            add(p, row.get('collection', 'Imported'))
        if len(data) > args.max_products: state.warn('Product limit reached; this is a partial import.')
    else:
        if not source_list: parser.error('Config must contain at least one source')
        http = HTTP(delay=args.delay)
        for source in source_list:
            summary = {'url': source['url'], 'name': source.get('name', source['url']), 'complete': False, 'seen': 0}
            report['sources'].append(summary)
            try:
                if len(found) >= args.max_products or state.pages >= args.max_pages:
                    state.warn('Configured limits stopped remaining sources; this is a partial catalog.')
                    continue
                for p in extract(http, source, state):
                    summary['seen'] += 1
                    add(p, summary['name'])
                    if len(found) >= args.max_products:
                        state.warn('Product limit reached; this is a partial catalog.')
                        break
                else:
                    summary['complete'] = summary['seen'] > 0 and not any('Page limit' in w for w in state.warnings)
                    if not summary['seen']:
                        report['errors'].append({'url': source['url'], 'error': 'No products were found at this source'})
            except Exception as e:
                report['errors'].append({'url': source['url'], 'error': str(e)})
            print(f"{summary['name']}: {summary['seen']} records, {len(found)} unique", flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    report['pages_fetched'] = state.pages
    report['product_count'] = len(found)
    items = list(found.values())
    if not items:
        report['status'] = 'failed'
        report['errors'].append({'error': 'No products found. Try a supported adapter, CSS selectors, or normalized JSON import.'})
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('No products extracted; see report.json', file=sys.stderr)
        return 1
    print(f'Downloading {len(items)} product images...', flush=True)
    report['image_errors'] = download_images(items, args.output, args.workers)
    report['offline_images'] = sum(bool(p['image']) for p in items)
    report['status'] = 'partial' if report['errors'] or report['image_errors'] or any('limit' in w.lower() for w in state.warnings) else 'complete_for_requested_sources'
    (args.output / 'products.json').write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    write_html(items, report, args.output)
    if args.pdf:
        try:
            make_pdf(items, report, args.output / 'catalog.pdf', args.font)
        except Exception as e:
            report['errors'].append({'error': 'PDF export failed: ' + str(e)})
            report['status'] = 'partial'
            write_html(items, report, args.output)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'READ-ME.txt').write_text('Unzip the entire folder, then open index.html in a modern browser. Keep pictures next to index.html.\nFor phones, save catalog.pdf locally and open it in a PDF reader.\nAll linked original product pages require internet. Prices are snapshots. Check report.json for coverage and missing assets.\n', encoding='utf-8')
    archive = package(args.output)
    print(json.dumps({'products': len(items), 'images': report['offline_images'], 'status': report['status'], 'archive': archive}, ensure_ascii=False))
    # Configured limits are intentional; network/export failures produce a non-zero result.
    return 2 if report['errors'] or report['image_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
