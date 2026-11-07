import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from PIL import Image
from offline_catalog.adapters import amount, product, web_url, State, shopify, skims, generic, decode_router, jsonld_product
from offline_catalog.render import write_html, make_pdf, package
from offline_catalog.cli import main


class FakeHTTP:
    def __init__(self, pages): self.pages, self.urls = pages, []
    def get(self, url, binary=False):
        self.urls.append(url)
        value = self.pages(url) if callable(self.pages) else self.pages[url]
        if isinstance(value, Exception): raise value
        return value


def router_page(route):
    table = []
    def encode(value):
        i = len(table); table.append(None)
        if isinstance(value, dict):
            table[i] = {'_' + str(encode(k)): encode(v) for k, v in value.items()}
        elif isinstance(value, list):
            table[i] = [encode(v) for v in value]
        else: table[i] = value
        return i
    encode({'loaderData': {'route': route}})
    return '<script>window.__reactRouterContext.streamController.enqueue(' + json.dumps(json.dumps(table)+'\n') + ');</script>'


class AdapterTests(unittest.TestCase):
    def test_price_unknown_is_not_zero(self):
        self.assertIsNone(amount(None)); self.assertIsNone(amount('NaN'))
        self.assertIsNone(amount('-2')); self.assertEqual(amount('0.00'), 0)
        self.assertEqual(amount('23.50'), 23.5)

    def test_url_and_currency_identity(self):
        with self.assertRaises(ValueError): web_url('javascript:alert(1)')
        with self.assertRaises(ValueError): web_url('https://user:secret@example.com')
        usd = product('https://shop.test/p', 'A', currency='USD')
        eur = product('https://shop.test/p', 'A', currency='EUR')
        self.assertNotEqual(usd['id'], eur['id'])
        self.assertIsNone(usd['price'])

    def test_shopify_pages_and_ranges(self):
        def p(i): return {'id': i, 'handle': str(i), 'title': 'P'+str(i), 'images': [{'src': 'https://img.test/a.jpg'}], 'variants': [{'title': 'S', 'price': '10', 'available': False}, {'title': 'M', 'price': '15', 'available': True}]}
        def response(url):
            if url.endswith('/cart.js'): return '{"currency":"USD"}'
            return json.dumps({'products': [p(i) for i in range(250)] if 'page=1' in url else [p(251)]})
        state = State(); items = list(shopify(FakeHTTP(response), {'url':'https://shop.test/collections/a'}, state))
        self.assertEqual(len(items), 251); self.assertEqual(state.pages, 2)
        self.assertEqual(items[0]['price'], 10); self.assertEqual(items[0]['max_price'], 15)
        self.assertTrue(items[0]['available']); self.assertEqual(items[0]['currency'], 'USD')

    def test_repeated_shopify_page_fails(self):
        rows = [{'id': i, 'handle': str(i), 'title': str(i)} for i in range(250)]
        http = FakeHTTP(lambda u: '{"currency":"USD"}' if u.endswith('/cart.js') else json.dumps({'products':rows}))
        with self.assertRaisesRegex(ValueError, 'repeated'):
            list(shopify(http, {'url':'https://shop.test'}, State()))

    def test_skims_router_and_pagination(self):
        card = {'product': {'handle': 'one', 'productType':'TOP'}, 'title':{'full':'One'}, 'priceRange':{'minVariantPrice':{'amount':'18','currencyCode':'USD'},'maxVariantPrice':{'amount':'18'}},'firstImage':{'url':'https://img.test/one.jpg'},'availableForSale':False}
        first = {'collection':{'handle':'a'},'products':[card],'paginationInfo':{'hasNextPage':True,'endCursor':'next'}}
        second = {'collection':{'handle':'a'},'products':[],'paginationInfo':{'hasNextPage':False}}
        http = FakeHTTP(lambda u: router_page(second if 'cursor=' in u else first))
        state = State(); rows = list(skims(http, {'url':'https://skims.com/collections/a'}, state))
        self.assertEqual(rows[0]['price'],18); self.assertFalse(rows[0]['available'])
        self.assertEqual(rows[0]['variants'],[]); self.assertEqual(state.pages,2)
        self.assertIn('listing-visible',state.warnings[0])

    def test_jsonld_aggregate_and_missing_currency(self):
        p = jsonld_product({'name':'A','url':'https://x.test/p','image':['/a.jpg'],'offers':{'lowPrice':'12','highPrice':'19','availability':'https://schema.org/InStock'}},'https://x.test/p')
        self.assertEqual(p['image_source'],'https://x.test/a.jpg')
        self.assertEqual(p['price'],12); self.assertEqual(p['max_price'],19)
        self.assertIsNone(p['currency']); self.assertTrue(p['available'])

    def test_jsonld_multi_currency_not_merged(self):
        with self.assertRaisesRegex(ValueError,'multiple currencies'):
            jsonld_product({'offers':[{'price':'10','priceCurrency':'USD'},{'price':'8','priceCurrency':'EUR'}]},'https://x.test/p')

    def test_generic_discovers_only_same_host(self):
        listing='<a href="/products/one">One</a><a href="https://other.test/products/no">No</a>'
        detail='<script type="application/ld+json">'+json.dumps({'@graph':[{'@type':'Product','name':'One','offers':{'price':'3','priceCurrency':'USD'}}]})+'</script>'
        http=FakeHTTP({'https://x.test/':listing,'https://x.test/products/one':detail})
        rows=list(generic(http,{'url':'https://x.test/'},State()))
        self.assertEqual(len(rows),1); self.assertEqual(len(http.urls),2)

    def test_selectors_european_price(self):
        page='<div class="card"><h2>Example</h2><b>EUR 1.299,95</b><a href="/p/1">Open</a><img src="/a.jpg"></div>'
        source={'url':'https://x.test/','currency':'EUR','decimal_separator':',','selectors':{'card':'.card','title':'h2','price':'b','link':'a','image':'img'}}
        rows=list(generic(FakeHTTP(lambda u:page),source,State()))
        self.assertEqual(rows[0]['price'],1299.95); self.assertEqual(rows[0]['url'],'https://x.test/p/1')

    def test_selector_price_range_is_not_concatenated(self):
        source={'url':'https://x.test/','currency':'USD','selectors':{'title':'h2','price':'b'}}
        state=State()
        rows=list(generic(FakeHTTP(lambda u:'<h2>A</h2><b>$12 - $15</b>'),source,state))
        self.assertIsNone(rows[0]['price']); self.assertTrue(state.warnings)

    def test_relative_jsonld_url(self):
        row=jsonld_product({'name':'A','url':'/products/a'},'https://x.test/shop')
        self.assertEqual(row['url'],'https://x.test/products/a')

    def test_page_limit_is_reported(self):
        state=State(max_pages=1)
        self.assertTrue(state.page()); self.assertFalse(state.page()); self.assertTrue(state.warnings)


class OutputTests(unittest.TestCase):
    def test_html_does_not_execute_product_strings(self):
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)
            p=product('https://x.test/p','</script><script>alert(1)</script>',price=0,currency='USD')
            report={'title':'<unsafe>','fetched_at':'2026-01-01','status':'complete_for_requested_sources'}
            write_html([p],report,output)
            soup=BeautifulSoup((output/'index.html').read_text(encoding='utf-8'),'html.parser')
            parsed=json.loads(soup.find(id='catalog-data').string)
            self.assertEqual(parsed['products'][0]['title'],p['title'])
            self.assertEqual(soup.title.text,'<unsafe>')
            self.assertNotIn('<script>alert(1)</script>',str(soup))

    def test_cli_import_pdf_and_archive(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d); source=base/'data.json'; output=base/'catalog'
            source.write_text(json.dumps([{'url':'https://x.test/a','title':'A','price':12,'currency':'USD'}, {'url':'https://x.test/a','title':'A','price':11,'currency':'EUR'}]),encoding='utf-8')
            def images(items,folder,workers):
                (folder/'pictures').mkdir()
                for item in items:
                    item['image']='pictures/'+item['id']+'.jpg'
                    Image.new('RGB',(40,60),'white').save(folder/item['image'])
                return []
            with patch('offline_catalog.cli.download_images',side_effect=images):
                code=main(['--input',str(source),'--output',str(output),'--pdf'])
            self.assertEqual(code,0)
            report=json.loads((output/'report.json').read_text())
            self.assertEqual(report['product_count'],2); self.assertEqual(report['offline_images'],2)
            self.assertTrue((output/'catalog.pdf').read_bytes().startswith(b'%PDF'))
            self.assertTrue((base/'catalog-offline.zip').is_file())
            with self.assertRaises(SystemExit): main(['--input',str(source),'--output',str(output)])

    def test_empty_import_is_failure_not_fake_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); (p/'empty.json').write_text('[]')
            self.assertEqual(main(['--input',str(p/'empty.json'),'--output',str(p/'out')]),1)
            self.assertFalse((p/'out/index.html').exists())

    def test_source_failure_exports_report(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('offline_catalog.cli.extract',side_effect=ValueError('blocked')):
                code=main(['--url','https://x.test','--output',d])
            self.assertEqual(code,1)
            self.assertIn('blocked',(Path(d)/'report.json').read_text())


if __name__ == '__main__': unittest.main()
