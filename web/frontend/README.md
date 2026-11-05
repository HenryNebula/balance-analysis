# statementledger web UI — frontend

The web app's UI: a React SPA (Vite + TypeScript + Tailwind). `web/app.py`
serves the built bundle at `/` and acts as its JSON API — the two share
URLs, content-negotiated by Accept header.

```console
$ npm ci
$ npm run build    # -> dist/ — the bundle web/app.py serves
$ npm run dev      # Vite dev server; API requests proxy to 127.0.0.1:8765
```
