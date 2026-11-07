"""No shell interpolation of workflow inputs. CLI validates numeric bounds."""
import os
from offline_catalog.cli import main

preset = os.environ.get('CATALOG_PRESET', 'skims')
if preset not in ('alo', 'skims', 'custom'):
    raise SystemExit('Unknown preset')
args = ['--output', 'output/catalog', '--max-products', os.environ.get('CATALOG_MAX_PRODUCTS', '200'),
        '--max-pages', os.environ.get('CATALOG_MAX_PAGES', '50')]
if preset == 'custom':
    url = os.environ.get('CATALOG_URL', '').strip()
    if not url:
        raise SystemExit('Custom mode requires a public URL')
    args += ['--url', url, '--adapter', os.environ.get('CATALOG_ADAPTER', 'auto')]
else:
    args += ['--config', 'configs/' + preset + '.json']
if os.environ.get('CATALOG_PDF', 'true').lower() == 'true':
    args.append('--pdf')
raise SystemExit(main(args))
