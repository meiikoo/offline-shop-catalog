from __future__ import annotations
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit, urlencode
import requests
from bs4 import BeautifulSoup


def web_url(value, base=None):
    value = urljoin(base or '', str(value or ''))
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Expected an http(s) URL without embedded credentials')
    return urlunsplit((p.scheme, p.netloc, p.path or '/', p.query, ''))


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value).strip())
        return float(n) if n.is_finite() and n >= 0 else None
    except InvalidOperation:
        return None


def product(url, title, image=None, price=None, maximum=None, currency=None,
            available=None, kind='', variants=None):
    url = web_url(url)
    price = amount(price)
    maximum = amount(maximum)
    if maximum is not None and price is not None and maximum < price:
        raise ValueError('Maximum price is below minimum price')
    return {'id': hashlib.sha256((url + '|' + str(currency or '').upper()).encode()).hexdigest()[:20], 'url': url,
            'title': str(title or 'Untitled product').strip(),
            'image_source': web_url(image, url) if image else None,
            'image': None, 'price': price, 'max_price': maximum if maximum is not None else price,
            'currency': str(currency).upper() if currency else None,
            'available': available, 'type': str(kind or ''),
            'variants': variants or [], 'collections': []}


@dataclass
class State:
    max_pages: int = 50
    pages: int = 0
    warnings: list = field(default_factory=list)
    sources: list = field(default_factory=list)

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def page(self):
        if self.pages >= self.max_pages:
            self.warn('Page limit reached; this is a partial catalog.')
            return False
        self.pages += 1
        return True


class HTTP:
    def __init__(self, delay=0.4):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'OfflineShopCatalog/0.1 (+personal offline catalog)',
                                     'Accept-Language': 'en-US,en;q=0.9'})
        self.delay = delay
        self.last = 0.0

    def get(self, url, binary=False):
        url = web_url(url)
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            response = self.session.get(url, timeout=(15, 45), stream=True)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                response.close()
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            # Limit decoded responses as well as downloads; do not ingest arbitrary large files.
            limit = 25_000_000 if binary else 15_000_000
            chunks, size = [], 0
            try:
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Response exceeds safety size limit')
                    chunks.append(chunk)
            finally:
                response.close()
            body = b''.join(chunks)
            return body if binary else body.decode(response.encoding or 'utf-8', errors='replace')
        raise RuntimeError('Request retries exhausted')


def shopify(http, source, state):
    url = source['url'].rstrip('/')
    # A currency must come from the storefront or explicit source configuration, never from a $ sign.
    currency = source.get('currency')
    if not currency:
        try:
            currency = json.loads(http.get(urljoin(url, '/cart.js'))).get('currency')
        except (ValueError, requests.RequestException):
            state.warn('Store currency could not be read; prices are labelled with unknown currency.')
    base = url.split('?', 1)[0]
    if '/collections/' in base:
        endpoint = base + '/products.json'
    elif urlsplit(base).path in ('', '/'):
        endpoint = base.rstrip('/') + '/products.json'
    else:
        raise ValueError('Shopify adapter expects a store homepage or /collections/... URL')
    seen = set()
    page = 1
    while state.page():
        data = json.loads(http.get(endpoint + '?' + urlencode({'limit': 250, 'page': page})))
        items = data.get('products')
        if not isinstance(items, list):
            raise ValueError('Shopify products array is missing')
        if not items:
            return
        ids = tuple(str(p.get('id')) for p in items)
        if ids in seen:
            raise ValueError('Shopify returned a repeated page')
        seen.add(ids)
        for p in items:
            variants = p.get('variants') or []
            prices = [amount(v.get('price')) for v in variants]
            prices = [v for v in prices if v is not None]
            images = p.get('images') or []
            yield product(urljoin(url, '/products/' + p['handle']), p.get('title'),
                          images[0].get('src') if images else None,
                          min(prices) if prices else None, max(prices) if prices else None,
                          currency, any(v.get('available') is True for v in variants) if variants else None,
                          p.get('product_type'), [{'title': v.get('title'), 'price': amount(v.get('price')),
                                                  'available': v.get('available')} for v in variants])
        if len(items) < 250:
            return
        page += 1


def decode_router(html):
    # Decode data references only. Never eval executable scripts from a website.
    pattern = r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:\\.|[^"\\])*")\);'
    chunks = [json.loads(m) for m in re.findall(pattern, html)]
    first = next((s for s in chunks if s.startswith('[')), None)
    if not first:
        raise ValueError('React Router data stream not found; SKIMS adapter may need an update')
    table = json.loads(first.strip())
    cache = {}
    def decode(i):
        if i < 0:
            return None
        if i in cache:
            return cache[i]
        value = table[i]
        if not isinstance(value, (list, dict)):
            return value
        output = [] if isinstance(value, list) else {}
        cache[i] = output
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                output.extend(value)  # Preserve unresolved promises; do not execute or fabricate them.
            else:
                output.extend(decode(v) for v in value)
        else:
            for key, val in value.items():
                output[decode(int(key[1:]))] = decode(val)
        return output
    return decode(0)


def skims(http, source, state):
    base = source['url'].split('?', 1)[0].rstrip('/')
    if '/collections/' not in base:
        raise ValueError('SKIMS expects a /collections/... URL; use the SKIMS preset for multiple categories')
    url, seen = base, set()
    while state.page():
        decoded = decode_router(http.get(url))
        route = next((v for v in decoded.get('loaderData', {}).values()
                      if isinstance(v, dict) and v.get('collection') and isinstance(v.get('products'), list)), None)
        if route is None:
            raise ValueError('SKIMS collection structure changed')
        for card in route['products']:
            p = card['product']
            low = card['priceRange']['minVariantPrice']
            high = card['priceRange']['maxVariantPrice']
            image = card.get('firstImage') or card.get('featuredImage') or {}
            yield product(urljoin(url, '/products/' + p['handle']), card['title']['full'],
                          image.get('url'), low.get('amount'), high.get('amount'), low.get('currencyCode'),
                          card.get('availableForSale'), p.get('productType'))
        info = route['paginationInfo']
        if not info.get('hasNextPage'):
            state.warn('SKIMS coverage is listing-visible colors; complete size stock and deferred color variants are not included.')
            return
        cursor = info.get('endCursor')
        if not cursor or cursor in seen:
            raise ValueError('Missing or repeated SKIMS pagination cursor')
        seen.add(cursor)
        url = base + '?' + urlencode({'direction': 'next', 'cursor': cursor})


def schema_nodes(soup):
    def walk(value):
        if isinstance(value, dict):
            yield value
            for nested in value.values():
                yield from walk(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from walk(nested)
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(tag.string or tag.get_text()))
        except (ValueError, TypeError):
            continue


def jsonld_product(node, page_url):
    image = node.get('image')
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get('url') or image.get('contentUrl')
    offers = node.get('offers') or {}
    offers = offers if isinstance(offers, list) else [offers]
    currencies = {o.get('priceCurrency') for o in offers if isinstance(o, dict) and o.get('priceCurrency')}
    if len(currencies) > 1:
        raise ValueError('Product offers contain multiple currencies; use a region-specific URL')
    lows, highs, available = [], [], []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        low = amount(offer.get('price', offer.get('lowPrice')))
        high = amount(offer.get('highPrice', offer.get('price', offer.get('lowPrice'))))
        if low is not None:
            lows.append(low)
        if high is not None:
            highs.append(high)
        status = str(offer.get('availability', '')).split('/')[-1]
        if status in ('InStock', 'LimitedAvailability', 'OnlineOnly'):
            available.append(True)
        elif status in ('OutOfStock', 'SoldOut', 'Discontinued'):
            available.append(False)
    return product(web_url(node.get('url') or page_url, page_url), node.get('name'), image,
                   min(lows) if lows else None, max(highs) if highs else None,
                   next(iter(currencies), None), any(available) if available else None,
                   node.get('category') if isinstance(node.get('category'), str) else '')


def generic(http, source, state):
    queue, visited = [source['url']], set()
    origin = urlsplit(source['url']).netloc
    selectors = source.get('selectors')
    while queue and state.page():
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        soup = BeautifulSoup(http.get(url), 'html.parser')
        if selectors:
            cards = soup.select(selectors['card']) if selectors.get('card') else [soup]
            for card in cards:
                def text(key):
                    el = card.select_one(selectors[key]) if selectors.get(key) else None
                    return el.get_text(' ', strip=True) if el else None
                link = card.select_one(selectors['link']) if selectors.get('link') else None
                img = card.select_one(selectors['image']) if selectors.get('image') else None
                value = text('price')
                # Price separators must be explicitly configured; never guess 1.299 vs 1,299.
                if value:
                    tokens = re.findall(r'[0-9]+(?:[.,][0-9]+)*', value)
                    if len(tokens) != 1:
                        state.warn('Ambiguous CSS price; select one numeric price node at ' + url)
                        value = None
                    else:
                        value = tokens[0]
                    if value and source.get('decimal_separator', '.') == ',':
                        value = value.replace('.', '').replace(',', '.')
                    elif value:
                        value = value.replace(',', '')
                title = text('title')
                if title:
                    yield product(urljoin(url, link.get('href')) if link and link.get('href') else url,
                                  title, (img.get('src') or img.get('data-src')) if img else None,
                                  value, currency=source.get('currency'))
            next_link = soup.select_one(selectors['next']) if selectors.get('next') else None
            if next_link and next_link.get('href'):
                target = web_url(next_link['href'], url)
                if urlsplit(target).netloc == origin and target not in visited:
                    queue.append(target)
            continue
        for node in schema_nodes(soup):
            types = node.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if 'Product' in types:
                try:
                    yield jsonld_product(node, url)
                except ValueError as e:
                    state.warn(str(e) + ' at ' + url)
            if 'ListItem' in types:
                item = node.get('item')
                target = node.get('url') or (item.get('url') if isinstance(item, dict) else item)
                if isinstance(target, str):
                    try:
                        target = web_url(target, url)
                        if urlsplit(target).netloc == origin and target not in visited and target not in queue:
                            queue.append(target)
                    except ValueError:
                        pass
        for link in soup.select('a[href]'):
            href = link.get('href', '')
            # Follow explicit product routes and pagination, not every navigation or account link.
            if '/products/' in href or link.get('rel') == ['next']:
                try:
                    target = web_url(href, url)
                    if urlsplit(target).netloc == origin and target not in visited and target not in queue:
                        queue.append(target)
                except ValueError:
                    pass
    state.warn('Generic extraction covers discovered public JSON-LD product pages only; JavaScript-only or blocked pages need a site adapter or import.')


def extract(http, source, state):
    adapter = source.get('adapter', 'auto')
    if adapter == 'auto':
        host = urlsplit(source['url']).hostname or ''
        if host in ('skims.com', 'www.skims.com'):
            adapter = 'skims'
        elif host in ('aloyoga.com', 'www.aloyoga.com'):
            adapter = 'shopify'
        else:
            adapter = 'generic'
    if adapter not in ('shopify', 'skims', 'generic', 'selectors'):
        raise ValueError('Unknown adapter: ' + adapter)
    if adapter == 'selectors' and not source.get('selectors'):
        raise ValueError('selectors adapter requires CSS selector configuration')
    return {'shopify': shopify, 'skims': skims, 'generic': generic,
            'selectors': generic}[adapter](http, source, state)
