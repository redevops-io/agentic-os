// Harness for the OAuth-protected MCP Streamable-HTTP server, using the OFFICIAL TypeScript SDK as the client.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { UnauthorizedError } from '@modelcontextprotocol/sdk/client/auth.js';

export const ISSUER = process.env.ISSUER || 'http://localhost:28300';
export const MCP_URL = `${ISSUER}/api/agent-gateway/mcp-server/`;
export const REDIRECT = 'http://127.0.0.1:28399/callback';          // nothing listens here: the harness reads the redirect, as a native app would

/** The client's own state on disk: registration, tokens and PKCE verifier survive a process restart, as a real client's would. */
export class FileProvider {
  constructor(file, { name = 'oauth-harness', scope = '' } = {}) { this.file = file; this._name = name; this._scope = scope; this.lastAuthUrl = null; this.stateValue = null; }
  _read() { try { return JSON.parse(fs.readFileSync(this.file, 'utf8')); } catch { return {}; } }
  _write(o) { fs.writeFileSync(this.file, JSON.stringify(o, null, 1), { mode: 0o600 }); }
  get redirectUrl() { return REDIRECT; }
  get clientMetadata() {
    return { client_name: this._name, redirect_uris: [REDIRECT], grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'],
             token_endpoint_auth_method: 'none', ...(this._scope ? { scope: this._scope } : {}) };
  }
  state() { this.stateValue = crypto.randomBytes(12).toString('hex'); return this.stateValue; }
  clientInformation() { return this._read().client; }
  saveClientInformation(c) { this._write({ ...this._read(), client: c }); }
  tokens() { return this._read().tokens; }
  saveTokens(t) { this._write({ ...this._read(), tokens: t }); }
  redirectToAuthorization(url) { this.lastAuthUrl = url; }
  saveCodeVerifier(v) { this._write({ ...this._read(), verifier: v }); }
  codeVerifier() { return this._read().verifier; }
  invalidateCredentials(scope) { const o = this._read(); if (scope === 'all' || scope === 'tokens') delete o.tokens; if (scope === 'all' || scope === 'client') delete o.client; if (scope === 'all' || scope === 'verifier') delete o.verifier; this._write(o); }
}

const basic = (userpass) => 'Basic ' + Buffer.from(userpass).toString('base64');
/** The person who consents: an operator (Basic) or a tenant approver (Bearer). */
export function person({ basicAuth, bearer }) { return { Authorization: basicAuth ? basic(basicAuth) : `Bearer ${bearer}` }; }

/** The consent step a person performs in a browser: open the authorization URL signed in, read the page, answer it. Returns {code,state} or {error}. */
export async function consent(authUrl, who, { decision = 'approve', grants = null, origin = new URL(ISSUER).origin } = {}) {
  const page = await fetch(authUrl, { headers: who, redirect: 'manual' });
  const html = await page.text(); const referrerPolicy = page.headers.get('referrer-policy');
  if (page.status !== 200) return { error: `consent page ${page.status}`, body: html.slice(0, 200) };
  const ticket = /name="ticket" value="([^"]+)"/.exec(html)?.[1];
  const boxes = [...html.matchAll(/<input type="checkbox" name="grant" value="([^"]+)"( checked)?>/g)];
  const offered = boxes.map((m) => m[1]); const preChecked = boxes.filter((m) => m[2]).map((m) => m[1]);
  if (!ticket) return { error: 'no ticket on the consent page' };
  const body = new URLSearchParams(); body.set('ticket', ticket); body.set('decision', decision);
  for (const g of (grants ?? preChecked)) body.append('grant', g);
  const res = await fetch(`${ISSUER}/oauth/authorize`, { method: 'POST', headers: { ...who, 'content-type': 'application/x-www-form-urlencoded', Origin: origin }, body, redirect: 'manual' });
  const loc = res.headers.get('location');
  if (!loc) return { error: `no redirect (HTTP ${res.status})`, body: (await res.text()).slice(0, 200), offered, referrerPolicy };
  const u = new URL(loc);
  return { code: u.searchParams.get('code'), state: u.searchParams.get('state'), error: u.searchParams.get('error'), offered, referrerPolicy, location: u.origin + u.pathname };
}

/** Connect a client; if the server asks for authorization, run the OAuth flow with `who` consenting. Returns {client, transport, authorized}. */
export async function connectClient(provider, who, opts = {}) {
  let transport = new StreamableHTTPClientTransport(new URL(MCP_URL), { authProvider: provider });
  const client = new Client({ name: 'oauth-harness', version: '1.0.0' });
  try { await client.connect(transport); return { client, transport, authorized: false }; }
  catch (e) {
    if (!(e instanceof UnauthorizedError)) throw e;
    if (!provider.lastAuthUrl) throw new Error('the server asked for authorization but the SDK produced no authorization URL');
    const r = await consent(provider.lastAuthUrl, who, opts);
    if (!r.code) return { client: null, transport, authorized: false, consent: r };
    if (provider.stateValue && r.state !== provider.stateValue) throw new Error(`state mismatch: sent ${provider.stateValue}, got ${r.state}`);
    await transport.finishAuth(r.code);
    transport = new StreamableHTTPClientTransport(new URL(MCP_URL), { authProvider: provider });
    const c2 = new Client({ name: 'oauth-harness', version: '1.0.0' });
    await c2.connect(transport);
    return { client: c2, transport, authorized: true, consent: r };
  }
}

export const results = [];
/** `xfail`: a KNOWN defect, reported to the owner. It must fail; if it passes the run fails ("strict xfail") so the marker is removed with the fix. */
export function check(name, ok, detail = '', { xfail = false } = {}) {
  const good = xfail ? !ok : !!ok;
  results.push({ name, ok: good, observed: !!ok, xfail, detail: String(detail).slice(0, 300) });
  console.log((good ? (xfail ? 'XFAIL ' : 'PASS ') : (xfail ? 'XPASS (remove the xfail) ' : 'FAIL ')) + name + (detail ? ' - ' + String(detail).slice(0, 200) : ''));
  return good;
}
export async function attempt(fn) { try { return { value: await fn() }; } catch (e) { return { error: e }; } }
export async function http(method, url, { headers = {}, body = null, form = null } = {}) {
  const h = { ...headers }; let b;
  if (form) { b = new URLSearchParams(form); h['content-type'] = 'application/x-www-form-urlencoded'; } else if (body) { b = JSON.stringify(body); h['content-type'] = 'application/json'; }
  const r = await fetch(url, { method, headers: h, body: b, redirect: 'manual' }); let j = null; const t = await r.text(); try { j = JSON.parse(t); } catch {}
  return { status: r.status, json: j, text: t.slice(0, 300), location: r.headers.get('location') };
}

/** Consent as a PERSON in a real Chromium: the browser opens the authorization URL, signs in with HTTP Basic (what its own prompt does), reads the page and
 *  presses Allow. Nothing is forged: every header on the consent POST is the browser's own. Returns {code, state, post: headers of the consent POST}. */
export async function browserConsent(authUrl, { user, pass }, { decision = 'approve' } = {}) {
  const { chromium } = await import('playwright');
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({ httpCredentials: { username: user, password: pass } });
    const page = await ctx.newPage(); const posts = [];
    page.on('request', (r) => { if (r.method() === 'POST' && r.url().includes('/oauth/authorize')) posts.push(r.headers()); });
    // the redirect target is a native-app style loopback address nothing listens on: the browser's request for it is the answer
    const callback = new Promise((resolve, reject) => { page.on('request', (r) => { if (r.url().startsWith('http://127.0.0.1:28399/')) resolve(r.url()); }); setTimeout(() => reject(new Error('the browser was never redirected back to the client')), 15000); });
    callback.catch(() => {});
    await page.goto(authUrl.toString());
    await page.click(`button[value=${decision}]`);
    const u = new URL(await callback);
    return { code: u.searchParams.get('code'), state: u.searchParams.get('state'), error: u.searchParams.get('error'), post: posts[0] || null };
  } finally { await browser.close(); }
}
export async function connectViaBrowser(provider, creds, opts = {}) {
  let transport = new StreamableHTTPClientTransport(new URL(MCP_URL), { authProvider: provider });
  const client = new Client({ name: 'oauth-harness', version: '1.0.0' });
  try { await client.connect(transport); return { client, authorized: false }; }
  catch (e) {
    if (!(e instanceof UnauthorizedError)) throw e;
    const r = await browserConsent(provider.lastAuthUrl, creds, opts);
    if (!r.code) return { client: null, authorized: false, consent: r };
    await transport.finishAuth(r.code);
    transport = new StreamableHTTPClientTransport(new URL(MCP_URL), { authProvider: provider });
    const c2 = new Client({ name: 'oauth-harness', version: '1.0.0' }); await c2.connect(transport);
    return { client: c2, authorized: true, consent: r };
  }
}
