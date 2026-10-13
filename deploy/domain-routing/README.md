# Public domain routing

Current intended state (declared 2026-10-13):

- `histopilot.cn`: **canonical primary domain**. The application emits `<link rel="canonical">` pointing at this origin (`PUBLIC_BASE_URL`) on every indexable public page, regardless of which host served the request; robots.txt, sitemap.xml, llms.txt and llms-full.txt are also generated from it. Default language: Chinese.
- `histopilot.com`: international optimized network route serving **the same service and accounts**; it is not a separate site. There is no geo-redirect: the CN-country 302 plan was cancelled on 2026-10-13 (see `docs/followup/remaining-work-implementation-index-20261013.md`); both homepages instead expose explicit “中国线路 / 国际线路” links. Default language: English (shipped in the application image's `static/i18n.js`); a manually saved language choice takes precedence over the domain default.
- `pt.solarise94.fun`: **deprecated**. `homepc-nginx.conf` 308-redirects every application path to `https://histopilot.cn$request_uri` (path preserved); ACME challenge files remain reachable for certificate renewal.

Caveat: the in-repo `international-nginx.conf` still contains the cancelled CN geo-redirect block (`$hp_cloudflare_peer` / `$hp_redirect_cn` → 302 `.cn`). It has not been reworked to the state above; do not deploy it as-is, and remove the geo-redirect before it is next used. The trust configuration (CF-Connecting-IP / `set_realip_from` limited to official Cloudflare ranges, verified against `$realip_remote_addr`) is independent of routing and stays as is.

Deploy `homepc-nginx.conf` to homePC `~/.config/pt-edge/nginx.conf` (retains the existing separate `histopilot-cn.conf` include); deploy `international-nginx.conf` to the **active international origin** `/etc/nginx/conf.d/histopilot.com.conf` only after reconciling it with the cancelled-geo-redirect state above. Back up, syntax-check, then reload each nginx service.

The old-domain 308 redirect (pt.solarise94.fun → histopilot.cn) was explicitly chosen by the user after being informed that browser OPFS/localStorage and login cookies cannot move between these domains. Old files remain in that browser origin but are temporarily inaccessible through normal navigation. Do not clear site storage as part of this change. A future recovery operation can temporarily restore the old origin.
