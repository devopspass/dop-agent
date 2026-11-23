# Third-party licenses

DOP Agent itself is licensed under the Apache License 2.0 (see `LICENSE`, `NOTICE`).
This file lists third-party components. Python tables were generated with
`pip-licenses` from fresh virtual environments installed from the pinned
requirements files; re-generate them when pins change. Run
`scripts/check_licenses.sh` to verify that the core install contains no GPL/AGPL packages.

## Core runtime dependencies (`requirements.txt`)

| Package | Version | License | URL |
|---|---|---|---|
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License | https://github.com/aio-libs/aiohappyeyeballs |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| aiosignal | 1.4.0 | Apache Software License | https://github.com/aio-libs/aiosignal |
| annotated-doc | 0.0.7 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.16.0 | MIT | https://anyio.readthedocs.io/en/stable/versionhistory.html |
| attrs | 26.1.0 | MIT | https://www.attrs.org/en/stable/changelog.html |
| certifi | 2026.11.20 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://cffi.readthedocs.io/en/latest/whatsnew.html |
| charset-normalizer | 3.5.3 | MIT | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| click | 8.5.1 | BSD-3-Clause | https://github.com/pallets/click/ |
| croniter | 7.0.0 | MIT | https://github.com/pallets-eco/croniter |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| fastapi | 0.163.1 | MIT | https://github.com/fastapi/fastapi |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| github-copilot-sdk | 1.0.34 | MIT | https://github.com/github/copilot-sdk |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| httptools | 0.9.0 | MIT | https://github.com/MagicStack/httptools |
| httpx | 0.28.1 | BSD License | https://github.com/encode/httpx |
| httpx-sse | 0.4.3 | MIT | https://github.com/florimondmanca/httpx-sse |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| itsdangerous | 2.2.0 | BSD License | https://github.com/pallets/itsdangerous/ |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| mcp | 1.30.0 | MIT License | https://modelcontextprotocol.io |
| multidict | 7.2.1 | Apache-2.0 | https://github.com/aio-libs/multidict |
| opentelemetry-api | 1.45.2 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| propcache | 0.5.4 | Apache-2.0 | https://github.com/aio-libs/propcache |
| pycparser | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.14.4 | MIT | https://github.com/pydantic/pydantic |
| pydantic-settings | 2.16.3 | MIT | https://github.com/pydantic/pydantic-settings |
| pydantic_core | 2.50.5 | MIT | https://github.com/pydantic |
| PyJWT | 2.15.1 | MIT | https://github.com/jpadilla/pyjwt |
| python-dateutil | 4.0.0 | BSD-3-Clause | https://github.com/dateutil/dateutil |
| python-dotenv | 1.2.4 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| python-multipart | 0.0.34 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| PyYAML | 6.0.3 | MIT License | https://pyyaml.org/ |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| requests | 2.34.2 | Apache Software License | https://github.com/psf/requests |
| rpds-py | 2026.9.1 | MIT | https://github.com/crate-py/rpds |
| slack_bolt | 1.30.0 | MIT License | https://github.com/slackapi/bolt-python |
| slack_sdk | 3.48.0 | MIT License | https://github.com/slackapi/python-slack-sdk |
| sse-starlette | 3.7.1 | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| starlette | 1.10.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| typing-inspection | 0.4.5 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tzdata | 2026.6 | Apache-2.0 | https://github.com/python/tzdata |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| uvicorn | 0.54.0 | BSD-3-Clause | https://uvicorn.dev/ |
| uvloop | 0.23.0 | Apache Software License; MIT License | UNKNOWN |
| watchfiles | 1.3.0 | MIT License | https://github.com/samuelcolvin/watchfiles |
| websocket-client | 1.9.2 | Apache-2.0 | https://github.com/websocket-client/websocket-client |
| websockets | 17.4 | BSD-3-Clause | https://github.com/python-websockets/websockets |
| yarl | 1.25.1 | Apache-2.0 | https://github.com/aio-libs/yarl |

## Optional extra: render (`requirements-render.txt`)

> **Warning:** `pymupdf` is licensed under the **GNU AGPL-3.0** (or a commercial
> Artifex license). It is only installed with the optional `render` extra and is
> not part of the core install. See `NOTICE`.

| Package | Version | License | URL |
|---|---|---|---|
| brotli | 1.2.1 | MIT | https://github.com/google/brotli |
| cssselect2 | 0.10.1 | BSD License | https://doc.courtbouillon.org/cssselect2/ |
| fonttools | 4.68.0 | MIT | http://github.com/fonttools/fonttools |
| Markdown | 3.11.3 | BSD-3-Clause | https://Python-Markdown.github.io/ |
| nh3 | 0.3.8 | MIT | UNKNOWN |
| pillow | 13.0.0 | MIT-CMU | https://python-pillow.org |
| pydyf | 0.13.0 | BSD License | https://www.courtbouillon.org/pydyf |
| pymupdf | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License | https://github.com/pymupdf/pymupdf |
| pyphen | 0.18.1 | GNU General Public License v2 or later (GPLv2+); GNU Lesser General Public License v2 or later (LGPLv2+); Mozilla Public License 1.1 (MPL 1.1) | https://www.courtbouillon.org/pyphen |
| tinycss2 | 1.5.1 | BSD License | https://www.courtbouillon.org/tinycss2 |
| tinyhtml5 | 2.1.0 | MIT License | https://github.com/CourtBouillon/tinyhtml5 |
| weasyprint | 70.0 | BSD License | https://weasyprint.org/ |
| webencodings | 0.6.1 | BSD License | https://github.com/CourtBouillon/webencodings |
| zopfli | 0.4.3 | Apache Software License | https://github.com/fonttools/py-zopfli |

`pyphen` is multi-licensed (GPL-2.0+ / LGPL-2.1+ / MPL-1.1); it is used under the
MPL-1.1 / LGPL option as a WeasyPrint dependency.

## Optional extra: voice (`requirements-voice.txt`)

Only the main packages are listed; `openai-whisper` pulls a large transitive
set (torch and its dependencies), whose licenses can be listed with
`pip-licenses` after installing the extra.

| Package | Version | License | URL |
|---|---|---|---|
| more-itertools | 11.2.1 | MIT | https://github.com/more-itertools/more-itertools |
| numba | 0.68.1 | BSD License | https://numba.pydata.org |
| numpy | 2.5.5 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://numpy.org |
| openai-whisper | 20250625 | MIT | https://github.com/openai/whisper |
| tiktoken | 0.14.0 | MIT License

Copyright (c) 2022 OpenAI, Shantanu Jain

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
 | https://github.com/openai/tiktoken |
| torch | 2.15.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND ISC AND MIT AND Zlib | https://pytorch.org |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | https://tqdm.github.io |

## Vendored JavaScript / CSS (`webui/vendor/`)

Served locally, no CDN. Full license texts are in `webui/vendor/licenses/`.

| Library | Version | License | Vendored file | License text |
|---|---|---|---|---|
| marked | 12.0.2 | MIT | `webui/vendor/marked.umd.js` | `webui/vendor/licenses/marked-MIT.txt` |
| DOMPurify | 3.1.6 | MPL-2.0 OR Apache-2.0 (dual; used under Apache-2.0) | `webui/vendor/purify.min.js` | `webui/vendor/licenses/dompurify-MPL-2.0-OR-Apache-2.0.txt` |
| highlight.js | 11.9.0 | BSD-3-Clause | `webui/vendor/highlight.min.js` | `webui/vendor/licenses/highlight.js-BSD-3-Clause.txt` |
| highlight.js github-dark theme | 11.9.0 | MIT (GitHub Inc.) | `webui/vendor/github-dark.min.css` | `webui/vendor/licenses/highlight.js-github-dark-theme-MIT.txt` |
| Chart.js | 4.4.7 | MIT | `webui/vendor/chart.umd.min.js` | `webui/vendor/licenses/chart.js-MIT.txt` |
| @kurkle/color (bundled in Chart.js) | 0.3.x | MIT | `webui/vendor/chart.umd.min.js` | `webui/vendor/licenses/kurkle-color-MIT.txt` |
| emoji-js | 3.7.0 | MIT | `webui/vendor/emoji.js` | `webui/vendor/licenses/emoji-js-MIT.txt` |
| mermaid | 11.4.1 | MIT | `webui/vendor/mermaid.min.js` | `webui/vendor/licenses/mermaid-MIT.txt` |

### Mermaid bundled dependencies

`mermaid.min.js` is a bundle of mermaid 11.4.1 and its npm dependencies, among them
DOMPurify 3.2.1 (MPL-2.0 OR Apache-2.0), js-yaml 4.1.0 (MIT), d3 modules (ISC / BSD-3-Clause),
dagre-d3 and dagre (MIT), cytoscape and its layout plugins (MIT), KaTeX (MIT), dayjs (MIT),
lodash-es (MIT), khroma (MIT), chevrotain / langium (Apache-2.0 / MIT), robust-predicates (Unlicense).
Bundle headers confirm DOMPurify 3.2.1, js-yaml 4.1.0, and the Bezier / Runge-Kutta
spring helpers (MIT). Because the bundle is minified, the notices for the complete npm
dependency tree of mermaid@11.4.1 (140 packages; licenses: MIT: 92, ISC: 33, Apache-2.0: 6, BSD-3-Clause: 6, see text: 1, (MPL-2.0 OR Apache-2.0): 1, Unlicense: 1)
are reproduced in `webui/vendor/licenses/mermaid-bundled-dependencies.txt`.
This is a superset of what the bundler actually included.

## Icons

Brand logos in `webui/icons/` and `drivers/*/icon.svg` are trademarks of their
respective owners, used for identification only (see `NOTICE`).
`webui/icons/logo.png` is the project logo and is owned by the project owner.
