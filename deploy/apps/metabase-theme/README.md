# Metabase theme overlay (ReDevOps a11y + brand)

Metabase's built-in whitelabel / custom-CSS is an **Enterprise-only** feature, so the open-source image the
apps stack ships (`metabase/metabase`) can't be themed through the product. This overlay themes it anyway, at
the edge, without forking Metabase: an `nginx` sidecar proxies Metabase and injects one stylesheet.

## What it does
- **`default.conf.template`** — nginx `sub_filter` adds `<link rel="stylesheet" href="/mb-theme.css">` to the
  HTML shell and serves the CSS; everything else (the SPA, its API, websockets) passes through untouched.
- **`theme.css`** — overrides Metabase's own `--mb-color-brand` token to ReDevOps teal (`#0E7490`, ~4.7:1 on
  white vs. the stock `#509EE2` ~2.6:1) and adds link underlines + a 24px target size.

## Why
The UI audit (`agentic-tests ui-agent audit-ui`) flagged the Metabase sign-in/setup screen on three WCAG points:
`color-contrast` (h1 + in-text links), `link-in-text-block` (links distinguishable by colour only), and
`WCAG 2.5.8 target-size`. With the overlay the same audit goes from **accessibility 1/3 (index 77.8)** to
**3/3 (index 100.0)**, zero violations.

## Use
```bash
docker compose -f compose.yml -f compose.metabase-theme.yml up -d
```
The published Metabase port (`METABASE_PORT`, default 3001) is then served through the proxy. Omit the second
`-f` and the stack is exactly the default, unthemed Metabase. Edit `theme.css` to re-skin — nginx serves it live
(no rebuild). This is a presentation overlay only; it changes no Metabase behaviour or data.
