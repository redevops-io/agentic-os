# certifi trust-chain compatibility: 2026.7.22 (frozen) versus 2026.10.19 (R0-prime)

Purpose: owner decision 6 (2026-11-04) keeps certifi 2026.10.19 unless a concrete trust-chain compatibility test shows a reason to change. This records that test.
Tool: `packaging/tools/certifi_chain_compat.py` (unit tests: `tests/test_certifi_chain_compat.py`). The measurement was run on 2026-11-04 (about 17:50 to 18:30 UTC) from one macOS arm64 host with the
product interpreter, CPython 3.12.10 / OpenSSL 3.0.16, through read-only TLS handshakes: one connection per host and bundle, SNI = the host name, hostname checking on, TLS 1.2 or newer, `cafile=` only
(the bundle under test is the only trust store), the connection closed after the handshake. No request, credential or POST was sent. Full per-handshake CSVs, the bundles and the command lines are in the
`rc2-certifi` folder of the agent reports (sha256 list `SHA256SUMS` there); this page keeps the conclusion and the product-host table.

## Conclusion

**No host validates with certifi 2026.7.22 and fails with 2026.10.19. No reason to change the pin was found: certifi 2026.10.19 is retained.**
OBSERVED for the 3,665 hosts tested (2,946 judged; the rest could not be reached by DNS or TCP and say nothing). Not shown: that no host anywhere depends on `emSign ECC Root CA - G3`.

## What was compared

| Bundle | sha256 | Certificates | Source |
|---|---|---|---|
| certifi 2026.7.22 (frozen v0.3.2) | `9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f` | 121 | PyPI wheel `certifi-2026.7.22` (wheel sha256 `62f22742...` = the value PyPI publishes and the frozen lock) |
| certifi 2026.10.19 (R0-prime) | `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785` | 120 | PyPI wheel `certifi-2026.10.19` (wheel sha256 `42d35340...` = PyPI and the R0-prime lock); **byte-identical to the `cacert.pem` extracted from the PyInstaller archive of the R0-prime candidate sidecar** `macos-arm64-unsigned-final2` (sidecar sha256 `2cb23701...`), asserted by `--expect-sha256` in the run |
| macOS system file, for contrast | `9dae8d76e55cb08991f2b672d58999ea15560d910759c16b544f843bdffbb994` | 128 | `/etc/ssl/cert.pem` of the test host (macOS 27.0.1; the exported system roots, not the keychain) |
| certifi 2026.10.18 and 2026.10.23, positive controls only | `d8788720...` (117), `2ef52ad5...` (118) | | PyPI |

The only difference between 2026.7.22 and 2026.10.19 (compared by certificate fingerprint): **2026.10.19 lacks `emSign ECC Root CA - G3`** (121 -> 120; nothing was added). 2026.10.19 keeps
`emSign Root CA - G1`, `emSign Root CA - C1` and `emSign ECC Root CA - C3`. The advisory (GHSA-6q48-6c7w-9gw2) says the G3 root is distrusted because intermediates chaining to it have compromised keys, so a host that fails
because its chain ends there is the intended effect of the removal, not an incompatibility to work around.

## Hosts (3,665)

| Category | Hosts | How chosen |
|---|---|---|
| product (code) | 38 | every host named in an `https://` literal under `agentic_os/` (not the minified UI bundle): provider endpoints and sign-up pages (the provider registry), hosted OAuth, GitHub, Telegram, Slack, Google, Microsoft, HubSpot, Apollo, x.ai, OpenRouter, the demo host and the pages the control plane links |
| product (connectors) | 24 | every host in the pinned `redevops-connectors` 0.1.0 package (Stripe, Polar, Postiz, Ayrshare, Blotato, Klaviyo, Brave, WhatsApp, Gmail ...) |
| documented | 5 (probed separately) | hosts named in `README.md`, `config.example.yaml`, `LICENSING.md` (`api.moonshot.ai`, `api.openai.com`, `demo.redevops.io`, `github.com`, `www.nvidia.com`); `docs/` names only loopback. All 5 validate with all three bundles (`results-documented.csv`). No updater or telemetry endpoint exists in the launcher or the sidecar |
| emSign candidates | 59 | eMudhra sites and Indian public-sector, banking and PKI-adjacent sites, to find chains that end in an emSign root |
| major-CA samples | 52 | sites of the large public CAs and of the less common roots in the bundle |
| Tranco top ranks | 1,994 | the first 2,000 ranks of the Tranco list downloaded 2026-11-04 (`top-1m.csv` sha256 `42e46f6f...`), minus duplicates |
| Tranco `.in` | 1,498 | the first 1,500 `.in` host names of the same list (eMudhra's main market) |

## Results

| | 2026.7.22 | 2026.10.19 | macOS system file |
|---|---|---|---|
| OK (verified) | 2,832 | 2,832 | 2,749 |
| VERIFY_FAIL | 94 | 94 | 177 |
| TLS_ERROR | 20 | 20 | 20 |
| NET_ERROR (no verdict: DNS, timeout, refused) | 719 | 719 | 719 |

* **Differences between 2026.7.22 and 2026.10.19: 0 hosts** (neither direction; every host has the same class and, where verified, the same verified root).
* Hosts whose verified chain ends in `emSign ECC Root CA - G3` under 2026.7.22: **0**. Hosts whose chain ends in `emSign Root CA - G1`: 19 (for example `icar.gov.in`, `mospi.gov.in`, `stanford.edu` and `jhu.edu` via InCommon),
  and all 19 still validate with 2026.10.19.
* The 94 VERIFY_FAIL and 20 TLS_ERROR hosts fail identically with both bundles (host-name mismatch for names the server does not serve, expired or self-signed certificates, incomplete chains, handshake refusals): not trust-store differences.
* The macOS system file fails 83 hosts that both certifi bundles validate (chains ending in Sectigo R46, GlobalSign R46, ISRG Root X2 and a few others that the exported system file lacks); this is the reason the product ships certifi at all.
* 442 of the NET_ERROR hosts did not resolve, 232 timed out, 34 refused the connection. A first pass at 48 workers produced local `Can't assign requested address` errors (ephemeral-port exhaustion on the test host); the 659 affected hosts
  were probed again with 6 workers and their rows replaced (`rerun-list` and `merge`), and no host is left with a mixed NET_ERROR/verdict result.
* **Product hosts: 61 of 62 validate with both certifi bundles; the 62nd, `connect.redevops.io` (a placeholder in the connectors' setup text), does not resolve in DNS for any bundle.** Verified roots are the ordinary public ones
  (Google Trust Services R1/R4, ISRG X1/X2, Amazon Root CA 1, DigiCert G2/G3, Sectigo E46, GoDaddy G2). No product host chains to an emSign root.
* **Positive control** (the harness can see a regression): the 19 emSign G1 hosts plus 5 control hosts against certifi 2026.10.18 (which removed every emSign root) give 19 regressions; against 2026.10.23 (G1 only) and 2026.10.19, 0.
  So a host that depends on a removed root is detected, and 2026.10.18 would have broken 19 of the hosts above (this is why 2026.10.19 is the better choice than 2026.10.18 for compatibility).

## If a product host were affected (not the case)

Options would be: keep 2026.10.19 and tell the host's operator to serve a chain to a retained root; pin 2026.7.22 again (which keeps a root the upstream advisory distrusts, GHSA-6q48-6c7w-9gw2, and breaks the
`verify-pins.py` deviation set); or ship a product-specific CA file for that one host. None is needed.

## Product hosts

| host | named in | 2026.7.22 | 2026.10.19 | verified root (2026.10.19) | macOS /etc/ssl/cert.pem |
|---|---|---|---|---|---|
| a.klaviyo.com | `redevops-connectors:providers/klaviyo.py` | OK | OK | GTS Root R4 | OK |
| accounts.google.com | `agentic-os:integrations/hosted_oauth.py` | OK | OK | GTS Root R4 | OK |
| aistudio.google.com | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| api.apollo.io | `agentic-os:world/connectors.py` | OK | OK | ISRG Root X2 | OK |
| api.ayrshare.com | `redevops-connectors:providers/ayrshare.py` | OK | OK | GTS Root R1 | OK |
| api.cerebras.ai | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| api.github.com | `agentic-os:world/gtm.py` | OK | OK | Sectigo Public Server Authentication Root E46 | OK |
| api.groq.com | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| api.hubapi.com | `agentic-os:world/connectors.py` | OK | OK | GTS Root R4 | OK |
| api.hunter.io | `agentic-os:world/enrichment.py` | OK | OK | GTS Root R4 | OK |
| api.openai.com | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| api.peopledatalabs.com | `agentic-os:world/enrichment.py` | OK | OK | Amazon Root CA 1 | OK |
| api.polar.sh | `redevops-connectors:providers/polar.py` | OK | OK | GTS Root R4 | OK |
| api.postiz.com | `redevops-connectors:providers/postiz.py` | OK | OK | Amazon Root CA 1 | OK |
| api.postmarkapp.com | `agentic-os:world/connectors.py` | OK | OK | Amazon Root CA 1 | OK |
| api.search.brave.com | `redevops-connectors:providers/web.py` | OK | OK | Amazon Root CA 1 | OK |
| api.slack.com | `redevops-connectors:setup.py` | OK | OK | ISRG Root X1 | OK |
| api.stripe.com | `redevops-connectors:providers/stripe.py` | OK | OK | DigiCert Assured ID Root G2 | OK |
| api.telegram.org | `agentic-os:channels.py` | OK | OK | Go Daddy Root Certificate Authority - G2 | OK |
| api.x.ai | `agentic-os:interaction/providers_grok.py` | OK | OK | GTS Root R4 | OK |
| app.apollo.io | `redevops-connectors:setup.py` | OK | OK | ISRG Root X2 | OK |
| app.hubspot.com | `redevops-connectors:setup.py` | OK | OK | GTS Root R4 | OK |
| app.postiz.com | `redevops-connectors:setup.py` | OK | OK | GTS Root R4 | OK |
| backend.blotato.com | `redevops-connectors:providers/blotato.py` | OK | OK | ISRG Root X1 | OK |
| brave.com | `redevops-connectors:setup.py` | OK | OK | Amazon Root CA 1 | OK |
| cdnjs.cloudflare.com | `agentic-os:mission/unified_shell.py` | OK | OK | GTS Root R4 | OK |
| chat.redevops.io | `agentic-os:control_plane.py` | OK | OK | GTS Root R4 | OK |
| cloud.cerebras.ai | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| connect.redevops.io | `redevops-connectors:setup.py` | NET_ERROR | NET_ERROR | gaierror: [Errno 8] nodename nor servnam | NET_ERROR |
| console.groq.com | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| console.x.ai | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| dashboard.peopledatalabs.com | `agentic-os:connections/registry.py` | OK | OK | ISRG Root X1 | OK |
| dashboard.stripe.com | `redevops-connectors:setup.py` | OK | OK | DigiCert Global Root G3 | OK |
| demo.redevops.io | `agentic-os:control_plane.py` | OK | OK | GTS Root R4 | OK |
| developers.facebook.com | `redevops-connectors:setup.py` | OK | OK | DigiCert Global Root G2 | OK |
| developers.google.com | `redevops-connectors:setup.py` | OK | OK | GTS Root R4 | OK |
| docs.googleapis.com | `agentic-os:integrations/google_app.py` | OK | OK | GTS Root R4 | OK |
| fonts.googleapis.com | `agentic-os:control_plane.py` | OK | OK | GTS Root R4 | OK |
| fonts.gstatic.com | `agentic-os:control_plane.py` | OK | OK | GTS Root R4 | OK |
| generativelanguage.googleapis.com | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| github.com | `agentic-os:control_plane.py` | OK | OK | Sectigo Public Server Authentication Root E46 | OK |
| gmail.googleapis.com | `redevops-connectors:providers/gmail.py` | OK | OK | GTS Root R4 | OK |
| graph.facebook.com | `agentic-os:interaction/channels_whatsapp.py` | OK | OK | DigiCert Global Root G2 | OK |
| graph.microsoft.com | `agentic-os:integrations/microsoft_app.py` | OK | OK | DigiCert Global Root G2 | OK |
| itunes.apple.com | `agentic-os:world/sponsorship.py` | OK | OK | DigiCert Global Root G2 | OK |
| login.microsoftonline.com | `agentic-os:integrations/hosted_oauth.py` | OK | OK | DigiCert Global Root G2 | OK |
| oauth2.googleapis.com | `agentic-os:integrations/hosted_oauth.py` | OK | OK | GTS Root R4 | OK |
| ollama.com | `agentic-os:mission/pair_real.py` | OK | OK | GTS Root R1 | OK |
| openrouter.ai | `agentic-os:mission/default_llm.py` | OK | OK | GTS Root R4 | OK |
| polar.sh | `redevops-connectors:setup.py` | OK | OK | GTS Root R4 | OK |
| postiz.com | `redevops-connectors:providers/postiz.py` | OK | OK | GTS Root R4 | OK |
| prospector.clearbit.com | `agentic-os:world/enrichment.py` | OK | OK | Amazon Root CA 1 | OK |
| redevops.io | `agentic-os:views.py` | OK | OK | GTS Root R4 | OK |
| sandbox-api.polar.sh | `redevops-connectors:providers/polar.py` | OK | OK | GTS Root R4 | OK |
| sheets.googleapis.com | `agentic-os:integrations/google_app.py` | OK | OK | GTS Root R4 | OK |
| slack.com | `agentic-os:channels.py` | OK | OK | ISRG Root X1 | OK |
| waha.devlike.pro | `redevops-connectors:providers/whatsapp_waha.py` | OK | OK | ISRG Root X1 | OK |
| www.ayrshare.com | `redevops-connectors:setup.py` | OK | OK | ISRG Root X1 | OK |
| www.blotato.com | `redevops-connectors:setup.py` | OK | OK | ISRG Root X1 | OK |
| www.googleapis.com | `agentic-os:integrations/productivity.py` | OK | OK | GTS Root R4 | OK |
| www.klaviyo.com | `redevops-connectors:setup.py` | OK | OK | GTS Root R4 | OK |
| www.peopledatalabs.com | `agentic-os:connections/registry.py` | OK | OK | ISRG Root X1 | OK |

## Limits (UNVERIFIED or not covered)

* 719 of 3,665 hosts could not be reached from the test host (DNS or TCP), so nothing is said about them; 2,946 were judged. A host that depends on `emSign ECC Root CA - G3` but is not among the 3,665 is not excluded.
  Certificate-transparency enumeration of every G3-issued certificate was not done.
* Verdicts are OpenSSL 3.0.16 path building with `X509_V_FLAG` defaults of Python's `create_default_context`, which is what the sidecar's `httpx`/`ssl` use with certifi; they are not a trace of the sidecar's own calls
  (which of its outbound connections use the certifi file is INFERRED, not traced). Other TLS stacks (WebView2, WKWebView, the system store) were not measured, apart from the exported macOS file.
* The result is a snapshot of 2026-11-04: servers change chains, and a retained root can later be removed upstream.
