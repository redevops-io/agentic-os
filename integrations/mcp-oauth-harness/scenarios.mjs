// Scenarios for OAuth over the real MCP Streamable-HTTP server, driven by the official TypeScript SDK. Needs WORK (the gateway's work dir) and WT (its worktree).
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { UnauthorizedError } from '@modelcontextprotocol/sdk/client/auth.js';
import { FileProvider, connectClient, connectViaBrowser, consent, person, check, attempt, http, results, ISSUER, MCP_URL } from './lib.mjs';

const W = process.env.WORK, WT = process.env.WT, PORT = new URL(ISSUER).port;
if (!W || !WT) { console.error('set WORK (the gateway work dir) and WT (the agentic-os checkout)'); process.exit(2); }
const sec = Object.fromEntries(fs.readFileSync(W + '/secrets.env', 'utf8').trim().split('\n').map((l) => l.split(/=(.*)/s).slice(0, 2)));
const operator = person({ basicAuth: sec.OPERATOR_BASIC_AUTH }), acme = person({ bearer: sec.APPROVER_ACME }), globex = person({ bearer: sec.APPROVER_GLOBEX });
const db = () => new DatabaseSync(W + '/oauth.db');
const fresh = (n) => { const f = `${W}/client-${n}.json`; fs.rmSync(f, { force: true }); return new FileProvider(f, { name: `harness ${n}` }); };
const connections = async (who) => (await http('GET', ISSUER + '/oauth/connections', { headers: who })).json;
const toolNames = async (c) => (await c.listTools()).tools.map((t) => t.name);
const callList = async (c) => { const r = await c.callTool({ name: 'projects.list', arguments: { arguments: {} } }); return r; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function restartGateway() {
  execFileSync(path.join(path.dirname(new URL(import.meta.url).pathname), 'stop_gateway.sh'), [PORT, W]);
  execFileSync(path.join(path.dirname(new URL(import.meta.url).pathname), 'start_gateway.sh'), [WT, W, PORT]);
  for (let i = 0; i < 60; i++) { const r = await attempt(() => fetch(ISSUER + '/.well-known/oauth-authorization-server')); if (r.value?.status === 200) return; await sleep(1000); }
  throw new Error('gateway did not come back');
}

// 1. discovery, registration, consent, PKCE, token, connect, list, call
const s1 = fresh('main'); let a;
{
  const un = await http('POST', MCP_URL, { body: { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} } });
  check('1.0 the MCP endpoint refuses an anonymous call and says how to authorize (401 + WWW-Authenticate resource metadata)', un.status === 401, un.status);
  const meta = await http('GET', ISSUER + '/.well-known/oauth-protected-resource');
  check('1.1 protected-resource metadata names the authorization server', meta.status === 200 && JSON.stringify(meta.json).includes(new URL(ISSUER).origin), JSON.stringify(meta.json)?.slice(0, 160));
  a = await connectClient(s1, operator);
  check('1.2 the SDK discovers, registers, obtains consent (PKCE S256), exchanges the code and connects', a.authorized && a.client, JSON.stringify(a.consent)?.slice(0, 160));
  check('1.3 the named grant dashboards.read is never pre-checked (offered or not)', a.consent && !(s1.tokens().scope || '').includes('dashboards.read'), `offered=${JSON.stringify(a.consent?.offered)}`);
  const names = await toolNames(a.client);
  check('1.4 the tools the granted scopes allow are listed, and no write or approval tool', names.length > 0 && !names.some((n) => /approv|decision|execute|block|delegate/.test(n) && n !== 'missions.list_pending_approvals'), names.join(','));
  check('1.5 the granted scope is read-only in the token response', !(s1.tokens().scope || '').includes('write'), s1.tokens().scope);
  const out = await attempt(() => callList(a.client));
  check('1.6 a read tool call succeeds through the OAuth-protected MCP server', out.value && !out.value.isError, JSON.stringify(out.value || out.error?.message)?.slice(0, 200));
  const sch = (await a.client.listTools()).tools[0].inputSchema;
  check('1.7 tools publish the capability input schema, not one opaque `arguments` object', !(Object.keys(sch.properties || {}).length === 1 && sch.properties.arguments), JSON.stringify(sch).slice(0, 160));
}

// 2. denial
{
  const p = fresh('deny'); const before = (await connections(operator))?.connections?.length ?? (await connections(operator))?.length;
  const r = await connectClient(p, operator, { decision: 'deny' });
  check('2.1 a refused consent returns access_denied to the client and no code', r.client === null && r.consent?.error === 'access_denied' && !r.consent?.code, JSON.stringify(r.consent)?.slice(0, 160));
  const after = (await connections(operator))?.connections?.length ?? (await connections(operator))?.length;
  check('2.2 a refused consent creates no connection', before === after, `${before} -> ${after}`);
}

// 3. restart persistence (server and client)
{
  await a.client.close();
  await restartGateway();
  const again = new FileProvider(W + '/client-main.json');          // a new process's view of the same client state
  const r = await connectClient(again, operator);
  check('3.1 after a server restart the stored token still works without a new consent', r.client && !r.authorized, r.authorized ? 'asked for consent again' : '');
  const out = r.client && (await attempt(() => callList(r.client)));
  check('3.2 and a call succeeds', out?.value && !out.value.isError, JSON.stringify(out?.value || out?.error?.message)?.slice(0, 160));
  if (r.client) await r.client.close();
}

// 4. expiry: an expired access token is refreshed by the SDK; an expired refresh token needs consent again
{
  const before = JSON.parse(fs.readFileSync(W + '/client-main.json')).tokens.access_token;
  const age = (tables) => { const d = db(); for (const t of tables) d.exec(`UPDATE ${t} SET exp = exp - 8640000, doc = json_set(doc, '$.exp', json_extract(doc, '$.exp') - 8640000)`); d.close(); };      // 100 days: the refresh token lives 30
  age(['access']);
  const p = new FileProvider(W + '/client-main.json'); const r = await attempt(() => connectClient(p, operator));
  const after = JSON.parse(fs.readFileSync(W + '/client-main.json')).tokens?.access_token;
  check('4.1 an expired access token is refreshed by the SDK with the refresh token, with no consent', r.value?.client && !r.value.authorized && after && after !== before, r.error?.message || (r.value?.authorized ? 'asked for consent' : ''));
  const c = r.value?.client; const o = c && (await attempt(() => callList(c))); check('4.2 the call after the refresh succeeds', o?.value && !o.value.isError, o?.error?.message);
  if (c) await c.close();
  age(['access', 'refresh']);
  const stored = JSON.parse(fs.readFileSync(W + '/client-main.json')); const cid0 = stored.client.client_id;
  const rawAccess = await http('POST', MCP_URL, { headers: { Authorization: 'Bearer ' + stored.tokens.access_token, Accept: 'application/json, text/event-stream' }, body: { jsonrpc: '2.0', id: 1, method: 'ping' } });
  check('4.3a the server refuses an expired access token', rawAccess.status === 401, rawAccess.status);
  const rawRefresh = await http('POST', ISSUER + '/oauth/token', { form: { grant_type: 'refresh_token', refresh_token: stored.tokens.refresh_token, client_id: cid0 } });
  check('4.3b the server refuses an expired refresh token', rawRefresh.status >= 400 && rawRefresh.json?.error, `${rawRefresh.status} ${JSON.stringify(rawRefresh.json)}`);
  const p2 = new FileProvider(W + '/client-main.json'); const r2 = await attempt(() => connectClient(p2, operator));
  check('4.3c with both expired, the SDK client is sent back to consent and ends up connected through a NEW authorization (not silently admitted)', r2.value?.authorized === true && r2.value?.client, r2.value ? `authorized=${r2.value.authorized} client=${!!r2.value.client} consent=${JSON.stringify(r2.value.consent)?.slice(0,80)}` : r2.error?.message);
  if (r2.value?.client) await r2.value.client.close();
}

// 5. revocation (by the person, in the app) ends access at the server and the refresh path
{
  const p = fresh('revoke'); const r = await connectClient(p, operator); const conns = (await connections(operator)); const list = conns.connections || conns;
  const mine = list.filter((x) => !x.revoked).sort((x, y) => (y.created_at || 0) - (x.created_at || 0))[0];
  check('5.0 the person can list the live connection', r.client && mine, JSON.stringify(mine)?.slice(0, 160));
  const tok = p.tokens(); const rev = await http('POST', `${ISSUER}/oauth/connections/${mine.connection_id || mine.id}/revoke`, { headers: { ...operator, Origin: new URL(ISSUER).origin }, body: {} });
  check('5.1 the person revokes it', rev.status < 300, `${rev.status} ${rev.text}`.slice(0, 120));
  const o = await attempt(() => callList(r.client));
  check('5.2 the next call on the same session is refused after revocation', o.error || o.value?.isError, o.error?.message || JSON.stringify(o.value)?.slice(0, 120));
  const rf = await http('POST', ISSUER + '/oauth/token', { form: { grant_type: 'refresh_token', refresh_token: tok.refresh_token, client_id: p.clientInformation().client_id } });
  check('5.3 the refresh token of a revoked connection is refused', rf.status >= 400 && rf.json?.error, `${rf.status} ${JSON.stringify(rf.json)}`);
  const p2 = new FileProvider(p.file); const r2 = await attempt(() => connectClient(p2, operator, { decision: 'deny' }));
  check('5.4 a new connect with the revoked credentials must go through consent again', r2.value?.client == null || r2.value?.authorized, r2.value ? JSON.stringify(r2.value.consent)?.slice(0, 100) : r2.error?.message);
}

// 6. tenant isolation, with the operator gate ON, and the difference between the deployment administrator and a tenant approver
{
  const anon = await fetch(ISSUER + '/oauth/connections');
  check('6.0 the outer operator gate is ON (an anonymous request is challenged for Basic credentials), so the next checks prove scoped OAuth works behind it', anon.status === 401 && /basic/i.test(anon.headers.get('www-authenticate') || ''), `${anon.status} ${anon.headers.get('www-authenticate')}`);
  const pa = fresh('acme'), pg = fresh('globex');
  const ra = await connectClient(pa, acme), rg = await connectClient(pg, globex);
  check('6.1 each tenant approver can consent for its own tenant', ra.client && rg.client, `${ra.consent?.error || ''} ${rg.consent?.error || ''}`);
  const la = (await connections(acme)), lg = (await connections(globex)); const A = la.connections || la, G = lg.connections || lg;
  const aIds = A.map((x) => x.connection_id || x.id), gIds = G.map((x) => x.connection_id || x.id);
  check('6.2 each approver sees only its own tenant\'s connections', A.length >= 1 && G.length >= 1 && aIds.every((i) => !gIds.includes(i)) && A.every((x) => x.tenant === 'acme') && G.every((x) => x.tenant === 'globex'), `acme ${aIds.length} globex ${gIds.length}`);
  const x = await http('POST', `${ISSUER}/oauth/connections/${aIds[0]}/revoke`, { headers: { ...globex, Origin: new URL(ISSUER).origin }, body: {} });
  check('6.3 one tenant\'s approver cannot revoke another tenant\'s connection', x.status >= 400, `${x.status} ${x.text}`.slice(0, 120));
  const still = await attempt(() => callList(ra.client)); check('6.4 and the connection still works afterwards', still.value && !still.value.isError, still.error?.message);
  const opsees = (await connections(operator)); const O = opsees.connections || opsees;
  check('6.5 (observation) the deployment OPERATOR (Basic) sees every tenant\'s connections; a tenant approver sees only its own; is that intended?', true, JSON.stringify(O.map((c) => c.tenant)));
  const adminRev = await http('POST', `${ISSUER}/oauth/connections/${gIds[0]}/revoke`, { headers: { ...operator, Origin: new URL(ISSUER).origin }, body: {} });
  check('6.6 the deployment administrator (operator) CAN end any tenant\'s connection', adminRev.status === 200, `${adminRev.status} ${adminRev.text}`.slice(0, 100));
  const afterAdmin = await attempt(() => callList(rg.client)); check('6.7 and that tenant\'s agent is then refused', afterAdmin.error || afterAdmin.value?.isError, afterAdmin.error?.message || '');
  const noCred = await http('GET', ISSUER + '/oauth/connections'); check('6.8 with no credential no connection list is returned', noCred.status === 401, noCred.status);
  const requester = person({ bearer: sec.REQUESTER_ACME });
  const reqList = await http('GET', ISSUER + '/oauth/connections', { headers: requester }); check('6.9 a tenant REQUESTER token is not an approver: it cannot list connections', reqList.status === 401 || reqList.status === 403, reqList.status);
  const reqConsent = await consent((await (async () => { const q = fresh('requester'); await attempt(() => connectClient(q, operator, { decision: 'deny' })); return q.lastAuthUrl; })()), requester); check('6.10 and cannot give consent (refused while approver tokens exist, by design)', !reqConsent.code, JSON.stringify(reqConsent)?.slice(0, 120));
  if (ra.client) await ra.client.close(); if (rg.client) await rg.client.close();
}

// 6b. consent from a real browser: under Referrer-Policy no-referrer a browser posts `Origin: null`; the fix is a same-origin policy so it posts its real Origin
{
  const ok = await attempt(async () => connectClient(fresh('browser-origin'), operator));
  check('6b.1 the consent page sends Referrer-Policy same-origin, so a browser posts its real Origin to the consent form', ok.value?.consent?.referrerPolicy === 'same-origin', `policy=${ok.value?.consent?.referrerPolicy}`);
  const nul = await attempt(async () => connectClient(fresh('origin-null'), operator, { origin: 'null' }));
  check('6b.2 (behaviour) a consent POST with Origin: null is still refused', nul.value?.authorized !== true, JSON.stringify(nul.value?.consent)?.slice(0, 120));
  const cross = await attempt(async () => connectClient(fresh('cross-origin'), operator, { origin: 'http://evil.example' }));
  check('6b.3 a consent POST from another site is refused', cross.value?.authorized !== true, JSON.stringify(cross.value?.consent)?.slice(0, 120));
}

// 6c. a REAL browser (Chromium) gives consent: its own headers, no fabricated Origin
{
  const [user, pass] = sec.OPERATOR_BASIC_AUTH.split(':');
  const p = fresh('browser'); const r = await attempt(() => connectViaBrowser(p, { user, pass }));
  check('6c.1 a real browser signs in, reads the consent page, presses Allow, and the SDK client connects', r.value?.authorized && r.value?.client, r.error?.message || JSON.stringify(r.value?.consent)?.slice(0, 160));
  const post = r.value?.consent?.post;
  check('6c.2 the consent POST carries the browser\'s real Origin (the deployment\'s own origin), not null and not forged', post?.origin === new URL(ISSUER).origin, `origin=${post?.origin}`);
  const o = r.value?.client && (await attempt(() => callList(r.value.client))); check('6c.3 the connected client can call a tool', o?.value && !o.value.isError, o?.error?.message);
  const den = await attempt(() => connectViaBrowser(fresh('browser-deny'), { user, pass }, { decision: 'deny' })); check('6c.4 pressing "Do not allow" in the browser returns access_denied to the client', den.value?.consent?.error === 'access_denied' && !den.value?.client, JSON.stringify(den.value?.consent)?.slice(0, 120) || den.error?.message);
  if (r.value?.client) await r.value.client.close();
}

// 8. input schemas are the capability's own, and malformed arguments are rejected the same way on every interface
{
  const p = fresh('schemas'); const r = await connectClient(p, operator); const tok = p.tokens().access_token; const H = { Authorization: 'Bearer ' + tok };
  const tools = (await r.client.listTools()).tools; const caps = (await http('GET', ISSUER + '/api/agent-gateway/capabilities', { headers: H })).json.capabilities;
  const same = caps.every((c) => { const t = tools.find((x) => x.name === c.name); const a = c.input_schema || {}; const b = t?.inputSchema || {}; return t && JSON.stringify(a.required || []) === JSON.stringify(b.required || []) && JSON.stringify(Object.keys(a.properties || {}).sort()) === JSON.stringify(Object.keys(b.properties || {}).sort()); });
  check('8.1 every MCP tool publishes the same input schema (properties and required) as the REST capability list', same && tools.length === caps.length, `${tools.length} tools, ${caps.length} capabilities`);
  const bad = [['sources.search', {}, 'a missing required property'], ['sources.search', { query: 5 }, 'a string given a number'], ['sources.search', { query: 'x', limit: 'many' }, 'an integer given a string'], ['projects.get_status', { project_id: 7 }, 'a string given a number']];
  for (const [name, args, why] of bad) {
    const rest = await http('POST', `${ISSUER}/api/agent-gateway/capabilities/${name}/invoke`, { headers: H, body: args });
    const restRejected = rest.status >= 400 || (rest.json?.status && !['ok', 'error'].includes(rest.json.status)) || rest.json?.status === 'invalid_arguments';
    const mcp = await attempt(() => r.client.callTool({ name, arguments: args })); const mcpRejected = !!mcp.error || mcp.value?.isError === true;
    check(`8.2 ${name} with ${why} is rejected by REST and by MCP alike`, restRejected && mcpRejected, `REST ${rest.status} ${rest.json?.status}; MCP ${mcp.error ? 'threw ' + mcp.error.code : 'isError=' + mcp.value?.isError}`, { xfail: true });   // KNOWN DEFECT: neither interface validates arguments against the input schema
  }
  await r.client.close();
}

// 7. protocol negatives over raw HTTP
{
  const reg = await http('POST', ISSUER + '/oauth/register', { body: { client_name: 'neg', redirect_uris: ['http://127.0.0.1:28399/callback'], token_endpoint_auth_method: 'none' } });
  const cid = reg.json?.client_id; const crypto = await import('node:crypto');
  const verifier = crypto.randomBytes(32).toString('base64url'); const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const q = (o) => ISSUER + '/oauth/authorize?' + new URLSearchParams({ response_type: 'code', client_id: cid, redirect_uri: 'http://127.0.0.1:28399/callback', code_challenge: challenge, code_challenge_method: 'S256', state: 's1', ...o });
  const anon = await http('GET', q({})); check('7.1 the consent page needs a signed-in person', anon.status === 401 || anon.status === 403, anon.status);
  const badRedirect = await http('GET', q({ redirect_uri: 'http://127.0.0.1:1/evil' }), { headers: operator }); check('7.2 an unregistered redirect_uri is refused and never redirected to', badRedirect.status >= 400 && !badRedirect.location, `${badRedirect.status} ${badRedirect.location || ''}`);
  const plain = await http('GET', q({ code_challenge_method: 'plain' }), { headers: operator }); check('7.3 PKCE method plain is refused', plain.status === 303 && /error=invalid_request/.test(plain.location || '') || plain.status >= 400, `${plain.status} ${plain.location || ''}`.slice(0, 140));
  const c1 = await consent(q({}), operator); const base = { grant_type: 'authorization_code', client_id: cid, redirect_uri: 'http://127.0.0.1:28399/callback' };
  const wrong = await http('POST', ISSUER + '/oauth/token', { form: { ...base, code: c1.code, code_verifier: crypto.randomBytes(32).toString('base64url') } });
  check('7.4 a wrong PKCE verifier is refused', wrong.status >= 400 && wrong.json?.error, `${wrong.status} ${JSON.stringify(wrong.json)}`);
  const c2 = await consent(q({}), operator);
  const ok = await http('POST', ISSUER + '/oauth/token', { form: { ...base, code: c2.code, code_verifier: verifier } });
  const replay = await http('POST', ISSUER + '/oauth/token', { form: { ...base, code: c2.code, code_verifier: verifier } });
  check('7.5 a correct exchange works once, and the same code is refused the second time', ok.status === 200 && replay.status >= 400, `${ok.status} then ${replay.status}`);
  const rfa = await http('POST', ISSUER + '/oauth/token', { form: { grant_type: 'refresh_token', client_id: cid, refresh_token: ok.json?.refresh_token } });
  const rfb = await http('POST', ISSUER + '/oauth/token', { form: { grant_type: 'refresh_token', client_id: cid, refresh_token: ok.json?.refresh_token } });
  check('7.6 a refresh token is single use (rotation): its second use is refused', rfa.status === 200 && rfb.status >= 400, `${rfa.status} then ${rfb.status}`);
  const afterReuse = await http('POST', MCP_URL, { headers: { Authorization: 'Bearer ' + rfa.json?.access_token }, body: { jsonrpc: '2.0', id: 1, method: 'ping' } });
  check('7.7 (observation) after reuse of a rotated refresh token the newer access token is', true, `HTTP ${afterReuse.status}`);
}

fs.writeFileSync(W + '/results.json', JSON.stringify({ sdk: JSON.parse(fs.readFileSync(new URL('./package.json', import.meta.url))).dependencies, issuer: ISSUER, results }, null, 1));
const bad = results.filter((r) => !r.ok); console.log(bad.length ? `${bad.length} FAILED` : `ALL PASS (${results.filter((r) => r.xfail).length} known defects still failing as expected)`);
process.exit(bad.length ? 1 : 0);
