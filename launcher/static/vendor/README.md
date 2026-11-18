# Vendored libraries for the launcher's /map page

The kit runs offline, so nothing loads from a CDN. These files are copied unchanged from the npm registry
tarballs (sha512 integrity checked against the registry):

| file | package | version | licence |
|---|---|---|---|
| `leaflet.js`, `leaflet.css` | [leaflet](https://www.npmjs.com/package/leaflet) `dist/` | 1.9.4 | BSD-2-Clause, `LICENSE-leaflet.txt` |
| `milsymbol.js` | [milsymbol](https://www.npmjs.com/package/milsymbol) `dist/` (UMD build, global `ms`) | 3.0.4 | MIT, `LICENSE-milsymbol.txt` |

To update: download the new tarball, check its integrity, copy the same files, bump `?v=` in `map.html`,
and update `tests/test_cop.py`'s pinned hashes.
