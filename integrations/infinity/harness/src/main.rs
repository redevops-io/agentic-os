//! Synthetic Infinity agent for the ReDevOps RAP bridge integration test.
//!
//! What is real: the Infinity local runtime (`infinity-agent-core` 0.2.0): its event processor, thread model, built-in `spawn_thread`, its
//! RAP tool dispatch (`RapTool` / `invoke_rap_tool`), its toolset loader, its callback-to-input conversion (`infinity-rap-bridge::prepare_callback`)
//! and its lifecycle notifier. What is scripted: the model (a deterministic provider; no LLM, no credentials). What this harness adds, because
//! the specification leaves authentication open and the stock local clients send none: an `HttpClient` that signs every request with
//! RAP-HMAC-SHA256-V1, and a callback receiver that verifies the bridge's signature, audience, tenant, expiry, nonce and event id before a
//! callback can reach an agent thread.
//!
//! Configuration is by environment variables set by the test (synthetic values only). Output is one JSON object per line on stdout.

use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use hmac::{Hmac, Mac};
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use infinity_agent_core::ThreadId;
use infinity_agent_core::message::{InputMessage, InputMessageContent};
use infinity_agent_core::stores::{InMemoryConversationStore, InMemoryStateStore};
use infinity_agent_core::system::{AgentEvent, AgentSystemBuilder, ReplaySnapshot, StaticModel, ThreadObserver};
use infinity_agent_core::tools::rap_tool::RapTool;
use infinity_agent_core::traits::InputSender;
use infinity_provider_protocol::completion::{CompletionError, CompletionRequest, FinalResponse, ModelStream, StreamChunk, Usage};
use infinity_provider_protocol::message::{Message, ToolCall, UserContent};
use infinity_provider_protocol::{ModelEntry, SingleModelProvider};
use rap_client::http::{HttpClient, InMemoryToolsetCache};
use rap_client::notifier::RapNotifier;
use rap_client::toolset_loader::ToolsetLoader;
use rap_protocol::RapCallback;
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;

type HmacSha256 = Hmac<Sha256>;
const SCHEME: &str = "RAP-HMAC-SHA256-V1";

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("missing environment variable {name}"))
}

fn now_secs() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).expect("clock").as_secs() as i64
}

fn emit(v: serde_json::Value) {
    println!("{v}");
}

fn hmac_hex(key: &[u8], msg: &[u8]) -> String {
    let mut m = HmacSha256::new_from_slice(key).expect("hmac key");
    m.update(msg);
    hex::encode(m.finalize().into_bytes())
}

fn hmac_raw(key: &[u8], msg: &[u8]) -> Vec<u8> {
    let mut m = HmacSha256::new_from_slice(key).expect("hmac key");
    m.update(msg);
    m.finalize().into_bytes().to_vec()
}

fn sha_hex(b: &[u8]) -> String {
    hex::encode(Sha256::digest(b))
}

fn canonical(method: &str, path: &str, audience: &str, key_id: &str, ts: i64, nonce: &str, body: &[u8], extra: &[(&str, String)]) -> String {
    let mut lines = vec![
        SCHEME.to_owned(), method.to_uppercase(), path.to_owned(), audience.to_owned(), key_id.to_owned(), ts.to_string(), nonce.to_owned(), sha_hex(body),
    ];
    let mut ex: Vec<_> = extra.iter().map(|(k, v)| format!("{k}:{v}")).collect();
    ex.sort();
    lines.extend(ex);
    lines.join("\n")
}

fn derive(secret: &[u8], direction: &str) -> Vec<u8> {
    hmac_raw(secret, format!("redevops-rap/v1/{direction}").as_bytes())
}

// ── runtime -> tool: an HttpClient that signs every request ─────────────────────────────────────────────────────────
#[derive(Debug)]
struct HarnessError(String);
impl std::fmt::Display for HarnessError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for HarnessError {}

#[derive(Clone)]
struct SigningClient {
    inner: reqwest::Client,
    key_id: String,
    secret: Vec<u8>,
    audience: String,
}

impl SigningClient {
    fn headers(&self, method: &str, url: &str, body: &[u8]) -> Vec<(&'static str, String)> {
        let path = reqwest::Url::parse(url).expect("url").path().to_owned();
        let ts = now_secs();
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let sig = hmac_hex(&derive(&self.secret, "inbound"), canonical(method, &path, &self.audience, &self.key_id, ts, &nonce, body, &[]).as_bytes());
        vec![("X-Rap-Key-Id", self.key_id.clone()), ("X-Rap-Timestamp", ts.to_string()), ("X-Rap-Nonce", nonce), ("X-Rap-Audience", self.audience.clone()), ("X-Rap-Signature", sig)]
    }
}

#[async_trait]
impl HttpClient for SigningClient {
    type Error = HarnessError;

    async fn post(&self, url: &str, body: &str) -> Result<u16, HarnessError> {
        let mut req = self.inner.post(url).header("content-type", "application/json").body(body.to_owned());
        for (k, v) in self.headers("POST", url, body.as_bytes()) {
            req = req.header(k, v);
        }
        Ok(req.send().await.map_err(|e| HarnessError(e.to_string()))?.status().as_u16())
    }

    async fn get(&self, url: &str) -> Result<(u16, Vec<u8>), HarnessError> {
        let mut req = self.inner.get(url).header("accept", "application/json");
        for (k, v) in self.headers("GET", url, b"") {
            req = req.header(k, v);
        }
        let resp = req.send().await.map_err(|e| HarnessError(e.to_string()))?;
        let st = resp.status().as_u16();
        Ok((st, resp.bytes().await.map_err(|e| HarnessError(e.to_string()))?.to_vec()))
    }
}

// ── tool -> runtime: a callback receiver that verifies before anything reaches a thread ─────────────────────────────
struct Verifier {
    secret: Vec<u8>,
    key_id: String,
    audience: String, // this receiver's own origin
    tenant: String,
    seen_nonce: Mutex<HashSet<String>>,
    seen_event: Mutex<std::collections::HashMap<String, String>>,
}

enum Verdict {
    Apply,
    Duplicate,
    Reject(u16, &'static str),
}

impl Verifier {
    fn verify(&self, path: &str, h: &hyper::HeaderMap, body: &[u8]) -> Verdict {
        let g = |n: &str| h.get(n).and_then(|v| v.to_str().ok()).unwrap_or("").to_owned();
        let (key, ts_s, nonce, sig, aud, event) = (g("x-rap-key-id"), g("x-rap-timestamp"), g("x-rap-nonce"), g("x-rap-signature"), g("x-rap-audience"), g("x-rap-event-id"));
        if key != self.key_id || sig.len() != 64 || nonce.is_empty() || event.is_empty() {
            return Verdict::Reject(401, "malformed");
        }
        let (Ok(ts), Ok(exp)) = (ts_s.parse::<i64>(), g("x-rap-expires").parse::<i64>()) else { return Verdict::Reject(401, "malformed") };
        let extra = [
            ("event", event.clone()), ("tenant", g("x-rap-tenant")), ("principal", g("x-rap-principal")), ("operation", g("x-rap-operation")),
            ("group", g("x-rap-group-id")), ("call", g("x-rap-tool-call-id")), ("expires", exp.to_string()),
        ];
        let want = hmac_hex(&derive(&self.secret, "callback"), canonical("POST", path, &self.audience, &self.key_id, ts, &nonce, body, &extra).as_bytes());
        if !constant_eq(want.as_bytes(), sig.to_lowercase().as_bytes()) {
            return Verdict::Reject(401, "bad_signature");
        }
        if aud != self.audience {
            return Verdict::Reject(401, "wrong_audience");
        }
        if g("x-rap-tenant") != self.tenant {
            return Verdict::Reject(403, "tenant");
        }
        let now = now_secs();
        if (now - ts).abs() > 60 || now >= exp {
            return Verdict::Reject(401, "stale_or_expired");
        }
        if !self.seen_nonce.lock().expect("lock").insert(nonce) {
            return Verdict::Reject(401, "replayed_nonce");
        }
        let digest = sha_hex(body);
        let mut events = self.seen_event.lock().expect("lock");
        match events.get(&event) {
            Some(prev) if *prev == digest => Verdict::Duplicate,
            Some(_) => Verdict::Reject(409, "event_id_reused"),
            None => {
                events.insert(event, digest);
                Verdict::Apply
            }
        }
    }
}

fn constant_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn reply(status: StatusCode) -> Response<Full<Bytes>> {
    Response::builder().status(status).body(Full::new(Bytes::new())).expect("response")
}

async fn serve_callbacks<S: InputSender + 'static>(listener: TcpListener, token_path: String, v: Arc<Verifier>, sender: S, rejected: Arc<Mutex<Vec<String>>>) {
    loop {
        let Ok((stream, _)) = listener.accept().await else { continue };
        let (v, sender, rejected, token_path) = (v.clone(), sender.clone(), rejected.clone(), token_path.clone());
        tokio::spawn(async move {
            let svc = hyper::service::service_fn(move |req: Request<Incoming>| {
                let (v, sender, rejected, token_path) = (v.clone(), sender.clone(), rejected.clone(), token_path.clone());
                async move {
                    let path = req.uri().path().to_owned();
                    let headers = req.headers().clone();
                    let body = match req.into_body().collect().await {
                        Ok(b) => b.to_bytes(),
                        Err(_) => return Ok::<_, std::convert::Infallible>(reply(StatusCode::BAD_REQUEST)),
                    };
                    if path != token_path {
                        rejected.lock().expect("lock").push("unknown_path".into());
                        return Ok(reply(StatusCode::NOT_FOUND));
                    }
                    match v.verify(&path, &headers, &body) {
                        Verdict::Reject(code, why) => {
                            rejected.lock().expect("lock").push(why.into());
                            emit(serde_json::json!({"callback": "rejected", "why": why}));
                            Ok(reply(StatusCode::from_u16(code).expect("status")))
                        }
                        Verdict::Duplicate => {
                            emit(serde_json::json!({"callback": "duplicate_dropped"}));
                            Ok(reply(StatusCode::OK))
                        }
                        Verdict::Apply => {
                            let cb: RapCallback = match serde_json::from_slice(&body) {
                                Ok(c) => c,
                                Err(_) => return Ok(reply(StatusCode::BAD_REQUEST)),
                            };
                            emit(serde_json::json!({"callback": "accepted", "type": match &cb {
                                RapCallback::ToolResult(_) => "tool_result", RapCallback::SubscriptionEvent(_) => "subscription_event",
                                RapCallback::OAuth(_) => "oauth", RapCallback::UserChoice(_) => "user_choice", RapCallback::ViewUpdate(_) => "view_update" }}));
                            if let infinity_rap_bridge::CallbackDelivery::Input { message, dedup_id } = infinity_rap_bridge::prepare_callback(cb) {
                                let _ = sender.send_to_input_queue(*message, &dedup_id).await;
                            }
                            Ok(reply(StatusCode::OK))
                        }
                    }
                }
            });
            let _ = http1::Builder::new().serve_connection(TokioIo::new(stream), svc).await;
        });
    }
}

// ── the scripted model ──────────────────────────────────────────────────────────────────────────────────────────────
#[derive(Clone)]
struct Script {
    scenario: String,
    tool: String,
    args: serde_json::Value,
    root: Arc<Mutex<String>>,
}

fn last_tool_result_text(req: &CompletionRequest) -> Option<String> {
    if let Some(Message::User { content }) = req.chat_history.last() {
        for c in content.iter() {
            if let UserContent::ToolResult(tr) = c {
                return Some(tr.content.iter().map(|x| format!("{x:?}")).collect::<Vec<_>>().join(" "));
            }
        }
    }
    None
}

fn first_user_text(req: &CompletionRequest) -> String {
    for m in req.chat_history.iter() {
        if let Message::User { content } = m {
            for c in content.iter() {
                if let UserContent::Text(t) = c {
                    return t.text.clone();
                }
            }
        }
    }
    String::new()
}

#[async_trait]
impl infinity_provider_protocol::CompletionModel for Script {
    async fn stream(&self, request: CompletionRequest) -> Result<ModelStream, CompletionError> {
        let mut chunks: Vec<Result<StreamChunk, CompletionError>> = Vec::new();
        let last = last_tool_result_text(&request);
        let is_child = format!("{:?}", request.chat_history).contains("child-task");
        let just_spawned = last.as_deref().is_some_and(|t| t.contains("newly created thread"));
        if let (true, true) = (is_child, just_spawned) {
            chunks.push(Ok(StreamChunk::ToolCall(ToolCall::new(uuid::Uuid::new_v4().to_string(), &self.tool, self.args.clone()))));
        } else if let Some(text) = last {
            chunks.push(Ok(StreamChunk::Text(format!("DONE after tool result: {text}"))));
        } else if first_user_text(&request).contains("child-task") {
            chunks.push(Ok(StreamChunk::ToolCall(ToolCall::new(uuid::Uuid::new_v4().to_string(), &self.tool, self.args.clone()))));
        } else if self.scenario == "child" {
            let root = self.root.lock().expect("lock").clone();
            chunks.push(Ok(StreamChunk::ToolCall(ToolCall::new(
                uuid::Uuid::new_v4().to_string(), "spawn_thread",
                serde_json::json!({"instructions": "child-task: call the governed tool", "child_of": [root], "fresh_context": true}),
            ))));
        } else {
            chunks.push(Ok(StreamChunk::ToolCall(ToolCall::new(uuid::Uuid::new_v4().to_string(), &self.tool, self.args.clone()))));
        }
        chunks.push(Ok(StreamChunk::Final(FinalResponse { usage: Some(Usage::default()) })));
        Ok(Box::pin(futures_util::stream::iter(chunks)))
    }
}

// ── observer: one JSON line per interesting event ───────────────────────────────────────────────────────────────────
struct Obs {
    done: Rc<RefCell<Vec<String>>>,
}

#[async_trait(?Send)]
impl ThreadObserver for Obs {
    type SubscribeRequest = ();

    fn on_event(&self, thread_id: &ThreadId<str>, event: &AgentEvent) {
        let t = thread_id.to_string();
        match event {
            AgentEvent::ToolCall { name, args, .. } => emit(serde_json::json!({"thread": t, "event": "tool_call", "name": name, "args": args})),
            AgentEvent::ToolResult { segments } => emit(serde_json::json!({"thread": t, "event": "tool_result", "segments": format!("{segments:?}")})),
            AgentEvent::TextChunk { text } if text.starts_with("DONE") => {
                emit(serde_json::json!({"thread": t, "event": "agent_finished", "text": text}));
                self.done.borrow_mut().push(t);
            }
            AgentEvent::Info { text } => emit(serde_json::json!({"thread": t, "event": "info", "text": text})),
            _ => {}
        }
    }

    fn on_subscribe(&self, _t: &ThreadId<str>, _r: (), _s: ReplaySnapshot) {}
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let bridge_url = env("HARNESS_BRIDGE_URL");
    let signer = SigningClient {
        inner: reqwest::Client::new(),
        key_id: env("HARNESS_KEY_ID"),
        secret: env("HARNESS_SECRET").into_bytes(),
        audience: env("HARNESS_AUDIENCE"),
    };
    let scenario = env("HARNESS_SCENARIO"); // "single" | "child"
    let expect_done = scenario_threads(&scenario);
    let root_cell = Arc::new(Mutex::new(String::new()));

    tokio::task::LocalSet::new()
        .run_until(async move {
            // 1. callback receiver on loopback with an unguessable path
            let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
            let port = listener.local_addr().expect("addr").port();
            let token_path = format!("/cb/{}", uuid::Uuid::new_v4().simple());
            let origin = format!("http://127.0.0.1:{port}");
            emit(serde_json::json!({"harness": "ready", "callback_origin": origin}));
            // the test reads the origin and allowlists it on the bridge before the first invocation: wait for its go-ahead on a marker file
            let go = env("HARNESS_GO_FILE");
            for _ in 0..600 {
                if std::path::Path::new(&go).exists() { break; }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }

            // 2. discover the bridge's toolset (signed GET), build the real RapTool objects
            let loader = ToolsetLoader::new(signer.clone(), InMemoryToolsetCache::new());
            let loaded = loader.load_toolsets(&[bridge_url.clone()], ThreadId::from_ref("harness-session")).await.expect("toolset discovery");
            let mut tools: Vec<Box<dyn infinity_agent_core::tools::Tool<infinity_agent_core::system::local::ChannelSender>>> = Vec::new();
            for ts in loaded {
                let endpoint = ts.manifest.endpoint.clone();
                for def in ts.manifest.tools {
                    emit(serde_json::json!({"discovered": def.name}));
                    tools.push(Box::new(RapTool { descriptor: def.into(), endpoint: endpoint.clone(), http_client: signer.clone(), callback_url: None }));
                }
            }

            // 3. the real runtime with a scripted model
            let entry = ModelEntry { model_id: "scripted".into(), display_name: "scripted".into(), context_window: 0, max_output_tokens: None, supports_image_input: false };
            let script = Script { scenario: scenario.clone(), tool: env("HARNESS_TOOL"), args: serde_json::from_str(&env("HARNESS_ARGS")).expect("args json"), root: root_cell.clone() };
            let provider = Arc::new(SingleModelProvider::new(entry.clone(), script));
            let model = StaticModel::from_entry(provider, &entry);
            let done = Rc::new(RefCell::new(Vec::<String>::new()));
            let d2 = done.clone();
            let running = AgentSystemBuilder::new_local(InMemoryConversationStore::new(), InMemoryStateStore::new(), model)
                .tools(tools)
                .callback_url(format!("{origin}{token_path}"))
                .rap_notifier(RapNotifier::new(vec![bridge_url.clone()], signer.clone()))
                .start_with_observer(move |_| Obs { done: d2.clone() });
            let sender = running.sender();

            let verifier = Arc::new(Verifier {
                secret: env("HARNESS_SECRET").into_bytes(), key_id: env("HARNESS_KEY_ID"), audience: origin.clone(), tenant: env("HARNESS_TENANT"),
                seen_nonce: Mutex::new(HashSet::new()), seen_event: Mutex::new(Default::default()),
            });
            let rejected = Arc::new(Mutex::new(Vec::<String>::new()));
            tokio::spawn(serve_callbacks(listener, token_path, verifier, sender.clone(), rejected.clone()));

            // 4. one user message starts the root thread; the agent calls the governed tool and the thread goes idle until the callback
            let root = "root-thread".to_owned();
            *root_cell.lock().expect("lock") = root.clone();
            sender.send_to_input_queue(InputMessage {
                content: InputMessageContent::User(UserContent::text("Please do the governed task.")),
                group_id: root.clone().into(), metadata: None, synthetic: None, display_as: None, subscription: false,
            }, &uuid::Uuid::new_v4().to_string()).await.expect("send");

            let mut running = running;
            let start = std::time::Instant::now();
            while done.borrow().len() < expect_done && start.elapsed() < Duration::from_secs(60) {
                tokio::time::sleep(Duration::from_millis(50)).await;
                while let Ok(ev) = running.try_next_lifecycle_event() {
                    emit(serde_json::json!({"lifecycle": format!("{ev:?}")}));
                }
            }
            let ok = done.borrow().len() >= expect_done;
            emit(serde_json::json!({"summary": {"finished_threads": done.borrow().clone(), "ok": ok, "callbacks_rejected": rejected.lock().expect("lock").clone()}}));
            // keep serving callbacks for a while so the test can attack the receiver (replay, forgery) and read the verdicts
            let hold: u64 = std::env::var("HARNESS_HOLD_S").ok().and_then(|v| v.parse().ok()).unwrap_or(0);
            tokio::time::sleep(Duration::from_secs(hold)).await;
            running.shutdown().await;
            std::process::exit(if ok { 0 } else { 2 });
        })
        .await;
}

fn scenario_threads(s: &str) -> usize {
    if s == "child" { 2 } else { 1 }
}
