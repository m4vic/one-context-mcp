import urllib.request
from urllib.error import HTTPError

names = ['one-ctx', 'octx-mcp', 'ctx-mcp', 'onectx', 'mcp-ctx']
for n in names:
    try:
        urllib.request.urlopen(f'https://pypi.org/pypi/{n}/json')
        print(f'{n}: TAKEN')
    except HTTPError:
        print(f'{n}: AVAILABLE')
