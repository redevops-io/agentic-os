#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-or-later
// rAIls local MCP bridge: a standard MCP server on stdio that forwards every call to one deployment's Governed Agent
// Gateway (cloud, on-premises or this machine). It exists for agents that speak MCP and need a local process: Claude
// Desktop (as an MCPB extension), and any other client that starts stdio MCP servers.
//
// It adds NO authority and holds no business state:
//   * the tool list is whatever the gateway shows THIS token (GET /api/agent-gateway/capabilities);
//   * a call is one POST to the gateway's governed path, where permission, budget, approval, egress policy and audit
//     apply exactly as for any other client; a refusal comes back as a tool error with the gateway's own words;
//   * the token comes from the environment (the host stores it in the OS keychain) and is sent only to the configured
//     deployment; it is never logged, and it is not sent in cleartext to anything but this machine.
// No dependency: Node's standard library only (Claude Desktop ships Node). Messages are newline-delimited JSON-RPC 2.0.
'use strict';
const http = require('node:http');
const https = require('node:https');
const readline = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');

const SERVER = { name: 'rails-gateway-bridge', version: '0.1.0' };
const PROTOCOLS = ['2025-06-18', '2025-03-26', '2024-11-05'];
const TIMEOUT_MS = Math.min(Math.max(Number(process.env.RAILS_TIMEOUT_MS) || 60000, 1000), 600000);
const log = (...a) => process.stderr.write('[rails-bridge] ' + a.join(' ') + '\n');

// A locally installed app publishes where it listens in <agents dir>/gateway.json while it runs, and keeps each connected
// agent's token in a file only the user can read. With RAILS_AGENTS_DIR and RAILS_TOKEN_FILE the bridge needs no address and
// no token in any client's settings: it finds the running app, and reads the token when it is used (so a revoked or renewed
// credential takes effect without restarting the client).
function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e && e.code === 'EPERM'; }
}

function discover(dir) {
  let doc;
  try { doc = JSON.parse(fs.readFileSync(path.join(dir, 'gateway.json'), 'utf8')); }
  catch (e) { return { error: 'the rAIls app is not running on this machine: open it and try again' }; }
  const url = String(doc && doc.url || '');
  if (!doc || doc.schema !== 'agentic-os.local-gateway/1' || !/^http:\/\/127\.0\.0\.1:\d{1,5}$/.test(url)) return { error: 'the local gateway file was not written by the rAIls app' };
  if (!alive(Number(doc.pid))) return { error: 'the rAIls app is not running on this machine: open it and try again' };
  return { url };
}

function config() {
  let raw = (process.env.RAILS_URL || '').trim();
  let token = (process.env.RAILS_TOKEN || '').trim();
  const agentsDir = (process.env.RAILS_AGENTS_DIR || '').trim();
  const tokenFile = (process.env.RAILS_TOKEN_FILE || '').trim();
  if (!raw && agentsDir) {
    const found = discover(agentsDir);
    if (found.error) return found;
    raw = found.url;
  }
  if (!token && tokenFile) {
    try { token = fs.readFileSync(tokenFile, 'utf8').trim(); }
    catch (e) { return { error: 'this agent is not connected to the rAIls app any more (its credential was removed): connect it again in the app' }; }
  }
  if (!raw) return { error: 'RAILS_URL is not set: the address of the rAIls deployment (for example https://apps.example.com or http://127.0.0.1:8787)' };
  if (!token) return { error: 'RAILS_TOKEN is not set: the agent token issued by that deployment' };
  let url;
  try { url = new URL(raw); } catch (e) { return { error: 'RAILS_URL is not a URL' }; }
  const local = ['127.0.0.1', 'localhost', '[::1]', '::1'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
    return { error: 'RAILS_URL must be https (plain http is accepted for this machine only): the token would travel in cleartext' };
  }
  if (url.username || url.password) return { error: 'RAILS_URL must not carry credentials' };
  const base = url.origin + url.pathname.replace(/\/+$/, '') + '/api/agent-gateway';
  return { base, token };
}

function call(cfg, method, path, body) {
  return new Promise((resolve) => {
    const u = new URL(cfg.base + path);
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const req = (u.protocol === 'https:' ? https : http).request(u, {
      method, timeout: TIMEOUT_MS,
      headers: { authorization: 'Bearer ' + cfg.token, accept: 'application/json', 'user-agent': SERVER.name + '/' + SERVER.version,
        ...(data ? { 'content-type': 'application/json', 'content-length': data.length } : {}) },
    }, (res) => {
      const chunks = []; let size = 0;
      res.on('data', (c) => { size += c.length; if (size <= 8 * 1024 * 1024) chunks.push(c); });
      res.on('end', () => {
        let json = null; const text = Buffer.concat(chunks).toString('utf8');
        try { json = JSON.parse(text); } catch (e) { /* not JSON */ }
        resolve({ status: res.statusCode, json, text: json ? '' : text.slice(0, 300) });
      });
    });
    req.on('timeout', () => req.destroy(new Error('the deployment did not answer in ' + TIMEOUT_MS + ' ms')));
    req.on('error', (e) => resolve({ status: 0, json: null, text: String(e.message || e) }));
    if (data) req.write(data);
    req.end();
  });
}

// MCP tool names allow [A-Za-z0-9_-]; capability names use dots. The mapping is rebuilt on every list and kept for calls.
let byTool = new Map();
const toolName = (cap) => cap.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64);

async function listTools(cfg) {
  const r = await call(cfg, 'GET', '/capabilities');
  if (r.status === 401 || r.status === 403) throw new Error('the deployment refused this token (HTTP ' + r.status + ')');
  if (r.status !== 200 || !r.json || !Array.isArray(r.json.capabilities)) throw new Error('the deployment did not return its capabilities (HTTP ' + r.status + (r.text ? ': ' + r.text : '') + ')');
  const next = new Map(); const tools = [];
  for (const c of r.json.capabilities) {
    if (!c || typeof c.name !== 'string') continue;
    const name = toolName(c.name);
    if (next.has(name)) { log('two capabilities map to the tool name', name, '- the second is not offered'); continue; }
    next.set(name, c.name);
    const schema = c.input_schema && typeof c.input_schema === 'object' && c.input_schema.type === 'object' ? c.input_schema : { type: 'object', properties: {}, additionalProperties: true };
    const notes = [c.kind ? 'kind: ' + c.kind : '', c.side_effecting ? 'changes data' : 'read only', c.approval && c.approval !== 'none' ? 'approval in the deployment: ' + c.approval : ''].filter(Boolean).join('; ');
    tools.push({ name, description: [c.description || c.name, notes ? '(' + notes + ')' : '', '[rAIls capability ' + c.name + ']'].filter(Boolean).join(' '), inputSchema: schema });
  }
  byTool = next;
  return tools;
}

async function callTool(cfg, params) {
  const name = params && params.name;
  if (!byTool.size) { try { await listTools(cfg); } catch (e) { return { isError: true, content: [{ type: 'text', text: String(e.message) }] }; } }
  const cap = byTool.get(name);
  if (!cap) return { isError: true, content: [{ type: 'text', text: 'unknown tool ' + JSON.stringify(name) + ': this token is not offered that capability' }] };
  const args = params.arguments && typeof params.arguments === 'object' && !Array.isArray(params.arguments) ? params.arguments : {};
  const r = await call(cfg, 'POST', '/capabilities/' + encodeURIComponent(cap) + '/invoke', args);
  if (!r.json) return { isError: true, content: [{ type: 'text', text: 'the deployment answered HTTP ' + r.status + (r.text ? ': ' + r.text : '') }] };
  const status = r.json.status;
  const failed = r.status >= 400 || (status !== undefined && status !== 'ok');
  // The gateway's own verdict is passed on unchanged: a denial or a pending approval is the answer, not something to retry around.
  return { isError: failed, content: [{ type: 'text', text: JSON.stringify(r.json) }], ...(failed ? {} : { structuredContent: r.json }) };
}

async function handle(msg, cfg) {
  const { id, method, params } = msg;
  const ok = (result) => ({ jsonrpc: '2.0', id, result });
  const err = (code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });
  if (method === 'initialize') {
    const asked = params && params.protocolVersion;
    return ok({ protocolVersion: PROTOCOLS.includes(asked) ? asked : PROTOCOLS[0], capabilities: { tools: { listChanged: false } }, serverInfo: SERVER,
      instructions: 'Tools are the capabilities one rAIls deployment grants this token. Every call is checked by that deployment (permission, budget, approval, egress policy) and recorded in its audit log. A denial or a pending approval is final here: report it to the person, do not look for another route.' });
  }
  if (method === 'ping') return ok({});
  if (id === undefined || id === null) return null;                       // a notification: nothing to answer
  if (cfg.error) return method === 'tools/call' ? ok({ isError: true, content: [{ type: 'text', text: cfg.error }] }) : err(-32002, cfg.error);
  try {
    if (method === 'tools/list') return ok({ tools: await listTools(cfg) });
    if (method === 'tools/call') return ok(await callTool(cfg, params || {}));
    if (method === 'resources/list') return ok({ resources: [] });
    if (method === 'prompts/list') return ok({ prompts: [] });
  } catch (e) {
    return err(-32603, String(e && e.message || e));
  }
  return err(-32601, 'method not found: ' + method);
}

function main() {
  const dynamic = !!((process.env.RAILS_AGENTS_DIR || '').trim() || (process.env.RAILS_TOKEN_FILE || '').trim());
  let cfg = config();
  if (cfg.error) log(cfg.error);
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  let chain = Promise.resolve();
  rl.on('line', (line) => {
    if (!line.trim()) return;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'parse error' } }) + '\n'); return; }
    chain = chain.then(() => { if (dynamic) cfg = config(); return handle(msg, cfg); }).then((out) => { if (out) process.stdout.write(JSON.stringify(out) + '\n'); })
      .catch((e) => log('internal error:', String(e && e.message || e)));
  });
  rl.on('close', () => chain.then(() => process.exit(0)));
}

if (require.main === module) main();
module.exports = { config, discover, toolName, handle };
