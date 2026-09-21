# Reddit provider policy (plan §29)

Reddit is a **distinct provider-policy surface**, not "another website to scrape." Every Reddit
capability in `external_agent_capabilities.yaml` is `CONTRACT_REQUIRED` / `POLICY_SCOPED` / `PROHIBITED`
and the gateway fails closed on anything not affirmatively enabled — it never falls back to browser
scraping.

Binding rules until a signed data agreement is recorded here:

- **Access** — use supported Reddit developer/data APIs only. Arbitrary scraping is NOT a production
  adapter path.
- **Provenance** — record the applicable Reddit policy/version identity on every `SocialObservation`
  (`provider_policy_ref`) and preserve required attribution / source references.
- **Commercial use** — treat lead-generation / commercial use of Reddit data as `CONTRACT_REQUIRED`:
  it needs explicit verification of contractual permission before enablement.
- **Training** — do not use Reddit content for model training unless permitted (`PROHIBITED` by default).
- **Outreach** — no unsolicited automated DMs (`social.send_dm` = `PROHIBITED`).

If commercial access is not authorized, the capability MUST fail closed. A capability is enabled only by
editing `external_agent_capabilities.yaml` to `POLICY_SCOPED`/`VERIFIED` **with** an evidence reference to
the agreement — never from provider behavior or marketing.
