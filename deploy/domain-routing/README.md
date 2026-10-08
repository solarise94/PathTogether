# Public domain routing

- `pt.solarise94.fun`: 308 to `https://histopilot.cn$request_uri` for every application path. ACME challenge files remain reachable for certificate renewal.
- `histopilot.cn`: Chinese default for a browser without a saved language choice.
- `histopilot.com`: English default; trusted Cloudflare country `CN` redirects GET/HEAD page requests to the matching `.cn` URL with 302 and `Cache-Control: no-store`. API, static assets, health checks and certificate challenges do not geo-redirect. Unknown country stays on `.com`.
- A manually saved language choice takes precedence over the domain default.

International nginx trusts `CF-IPCountry` only when the underlying connection peer (`$realip_remote_addr`) is in the configured Cloudflare ranges. A direct-origin caller cannot trigger geo-routing by forging that header. Preserve the existing CF-Connecting-IP trust configuration; review ranges when Cloudflare updates them.

Deploy `homepc-nginx.conf` to homePC `~/.config/pt-edge/nginx.conf` (retains the existing separate `histopilot-cn.conf` include); deploy `international-nginx.conf` to the LA host `/etc/nginx/conf.d/histopilot.com.conf`. Back up, syntax-check, then reload each nginx service. Language defaults are shipped in the application image's `static/i18n.js`.

The user explicitly chose a full old-domain redirect after being informed that browser OPFS/localStorage and login cookies cannot move between these domains. Old files remain in that browser origin but are temporarily inaccessible through normal navigation. Do not clear site storage as part of this change. A future recovery operation can temporarily restore the old origin.

Country routing follows Cloudflare IP geolocation, not browser language. Mainland China (`CN`) redirects; HK/MO/TW and unknown codes remain on `.com`. Reference: https://developers.cloudflare.com/network/ip-geolocation/ .
