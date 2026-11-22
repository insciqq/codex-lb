use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use base64::Engine as _;
use codex_lb_protocol::{NativeEvent, NativeRequest, NativeSseOptions};
use codex_lb_responses::compact::{CompactCollector, CompactResult};
use codex_lb_responses::stream::interpret;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

use crate::output::EventBatch;
use crate::runtime::{Output, RequestError, emit};
use crate::sse::{SseEventTooLarge, SseFramer, text_fragments};

pub(crate) const CODEX_H2_INITIAL_STREAM_WINDOW_SIZE: u32 = 2 * 1024 * 1024;
pub(crate) const CODEX_H2_INITIAL_CONNECTION_WINDOW_SIZE: u32 = 5 * 1024 * 1024;
pub(crate) const CODEX_H2_MAX_FRAME_SIZE: u32 = 16 * 1024;
pub(crate) const CODEX_H2_MAX_HEADER_LIST_SIZE: u32 = 16 * 1024;
const SSE_READ_CHUNK_SIZE: usize = 16 * 1024;
const SSE_IPC_TEXT_FRAGMENT_SIZE: usize = 16 * 1024;

#[derive(Debug)]
struct StreamIdleTimeout;

impl std::fmt::Display for StreamIdleTimeout {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("native upstream SSE body read timed out")
    }
}

impl std::error::Error for StreamIdleTimeout {}

/// Idle lifetime of a pooled upstream connection inside one client.
const POOL_IDLE_TIMEOUT: Duration = Duration::from_secs(120);
/// How long a client no request uses is kept. Building a client loads the
/// native root store on the command loop, so an idle account keeps its client
/// well past its connections; an unconnected client is cheap to keep.
const UNUSED_CLIENT_TTL: Duration = Duration::from_secs(30 * 60);
/// Unused clients kept at most, the one being leased included. A client with
/// a request in flight is never evicted, so the pool holds at most this many
/// clients plus those of the requests in flight.
const MAX_UNUSED_CLIENTS: usize = 256;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct ClientKey {
    /// Opaque per-account key from the adapter. Each key gets its own client
    /// and therefore its own connections, so a connection failure only fails
    /// the streams of the account that owns it.
    pub(crate) pool_key: Option<String>,
    pub(crate) proxy_url: Option<String>,
    pub(crate) connect_timeout_ms: Option<u64>,
    pub(crate) decode_response: bool,
}

/// When a pool entry was last used, shared with the leases of its requests.
struct Activity {
    used: Mutex<Instant>,
}

impl Activity {
    fn last_used(&self) -> Instant {
        *self.used.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn touch(&self, now: Instant) {
        let mut used = self.used.lock().unwrap_or_else(PoisonError::into_inner);
        if now > *used {
            *used = now;
        }
    }
}

/// Keeps a pooled client in use until the request that took it finishes.
pub(crate) struct ClientLease {
    activity: Arc<Activity>,
}

impl Drop for ClientLease {
    fn drop(&mut self) {
        self.activity.touch(Instant::now());
    }
}

pub(crate) struct PoolEntry {
    client: reqwest::Client,
    activity: Arc<Activity>,
}

impl PoolEntry {
    fn lease(&self, now: Instant) -> (reqwest::Client, ClientLease) {
        self.activity.touch(now);
        let lease = ClientLease {
            activity: Arc::clone(&self.activity),
        };
        (self.client.clone(), lease)
    }

    /// No request holds a lease: only the pool references the activity.
    fn unused(&self) -> bool {
        Arc::strong_count(&self.activity) == 1
    }

    fn expired(&self, now: Instant) -> bool {
        if !self.unused() {
            return false;
        }
        let idle_for = now.saturating_duration_since(self.activity.last_used());
        idle_for >= UNUSED_CLIENT_TTL
    }
}

pub(crate) struct ClientPool {
    pub(crate) clients: HashMap<ClientKey, PoolEntry>,
    max_unused: usize,
}

impl Default for ClientPool {
    fn default() -> Self {
        Self::with_max_unused(MAX_UNUSED_CLIENTS)
    }
}

impl ClientPool {
    fn with_max_unused(max_unused: usize) -> Self {
        Self {
            clients: HashMap::new(),
            max_unused,
        }
    }

    /// Return the client for `key` with a lease that keeps it pooled until
    /// the request is done.
    pub(crate) fn get(
        &mut self,
        key: &ClientKey,
    ) -> Result<(reqwest::Client, ClientLease), reqwest::Error> {
        self.get_at(key, Instant::now())
    }

    fn get_at(
        &mut self,
        key: &ClientKey,
        now: Instant,
    ) -> Result<(reqwest::Client, ClientLease), reqwest::Error> {
        self.evict_unused(key, now);
        if let Some(entry) = self.clients.get(key) {
            return Ok(entry.lease(now));
        }
        let entry = PoolEntry {
            client: build_client(key)?,
            activity: Arc::new(Activity {
                used: Mutex::new(now),
            }),
        };
        let leased = entry.lease(now);
        self.clients.insert(key.clone(), entry);
        Ok(leased)
    }

    /// Drop clients that no request uses: those unused for the client TTL,
    /// then the least recently used beyond the bound. `keep` is the client
    /// about to be leased; it counts toward the bound once its lease ends.
    fn evict_unused(&mut self, keep: &ClientKey, now: Instant) {
        let stale = |key: &ClientKey, entry: &mut PoolEntry| key != keep && entry.expired(now);
        self.clients.retain(|key, entry| !stale(key, entry));
        let evictable = |key: &ClientKey, entry: &PoolEntry| key != keep && entry.unused();
        let unused = self
            .clients
            .iter()
            .filter(|&(key, entry)| evictable(key, entry))
            .count();
        let others = self.max_unused.saturating_sub(1);
        if unused <= others {
            return;
        }
        let mut candidates: Vec<(Instant, ClientKey)> = self
            .clients
            .iter()
            .filter(|&(key, entry)| evictable(key, entry))
            .map(|(key, entry)| (entry.activity.last_used(), key.clone()))
            .collect();
        candidates.sort_unstable_by_key(|&(used, _)| used);
        for (_, key) in candidates.into_iter().take(unused - others) {
            self.clients.remove(&key);
        }
    }
}

fn build_client(key: &ClientKey) -> Result<reqwest::Client, reqwest::Error> {
    let mut builder = reqwest::Client::builder()
        .use_rustls_tls()
        .http2_initial_stream_window_size(CODEX_H2_INITIAL_STREAM_WINDOW_SIZE)
        .http2_initial_connection_window_size(CODEX_H2_INITIAL_CONNECTION_WINDOW_SIZE)
        .http2_max_frame_size(CODEX_H2_MAX_FRAME_SIZE)
        .http2_max_header_list_size(CODEX_H2_MAX_HEADER_LIST_SIZE)
        .pool_idle_timeout(POOL_IDLE_TIMEOUT)
        .pool_max_idle_per_host(8);
    if !key.decode_response {
        builder = builder.no_brotli().no_deflate().no_gzip().no_zstd();
    }
    if let Some(connect_timeout_ms) = key.connect_timeout_ms {
        builder = builder.connect_timeout(Duration::from_millis(connect_timeout_ms));
    }
    if let Some(proxy_url) = key.proxy_url.as_deref() {
        builder = builder.proxy(reqwest::Proxy::all(proxy_url)?);
    }
    builder.build()
}

pub(crate) async fn execute_request(
    request: NativeRequest,
    client: reqwest::Client,
    output: &Output,
) -> Result<NativeEvent, RequestError> {
    let sse = request.sse;
    let method = reqwest::Method::from_bytes(request.method.as_bytes())?;
    let headers = forwarded_headers(request.headers)?;
    let mut builder = client.request(method, request.url).headers(headers);
    if let Some(timeout_ms) = request.timeout_ms {
        builder = builder.timeout(Duration::from_millis(timeout_ms));
    }
    if let Some(encoded_body) = request.body {
        builder = builder.body(base64::engine::general_purpose::STANDARD.decode(encoded_body)?);
    }

    let mut response = builder.send().await?;
    let status = response.status().as_u16();
    let response_headers = response
        .headers()
        .iter()
        .map(|(name, value)| {
            (
                name.as_str().to_owned(),
                String::from_utf8_lossy(value.as_bytes()).into_owned(),
            )
        })
        .collect();
    emit(
        output,
        &NativeEvent::Head {
            request_id: request.request_id.clone(),
            status,
            http_version: format!("{:?}", response.version()),
            headers: response_headers,
        },
    )
    .await?;

    if let Some(options) = sse.filter(|options| {
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .map(|value| String::from_utf8_lossy(value.as_bytes()))
            .unwrap_or_default();
        status < 400
            && (!options.content_type_aware
                || content_type.is_empty()
                || content_type.split(';').next().is_some_and(|media_type| {
                    media_type.trim().eq_ignore_ascii_case("text/event-stream")
                }))
    }) {
        if let Some(error) =
            execute_sse_body(&mut response, &request.request_id, options, output).await?
        {
            return Ok(NativeEvent::SseEventTooLarge {
                request_id: request.request_id,
                size_bytes: error.size_bytes,
                limit_bytes: error.limit_bytes,
            });
        }
    } else {
        while let Some(chunk) = response.chunk().await? {
            emit(
                output,
                &NativeEvent::Chunk {
                    request_id: request.request_id.clone(),
                    data: base64::engine::general_purpose::STANDARD.encode(chunk),
                },
            )
            .await?;
        }
    }
    Ok(NativeEvent::End {
        request_id: request.request_id,
    })
}

async fn execute_sse_body(
    response: &mut reqwest::Response,
    request_id: &str,
    options: NativeSseOptions,
    output: &Output,
) -> Result<Option<SseEventTooLarge>, RequestError> {
    let idle_timeout = Duration::from_millis(options.idle_timeout_ms);
    let mut framer = SseFramer::new(options.max_event_bytes);
    let mut collector = options.collect_compact.then(CompactCollector::default);
    let mut batch = EventBatch::default();
    loop {
        let chunk = tokio::time::timeout(idle_timeout, response.chunk())
            .await
            .map_err(|_| StreamIdleTimeout)??;
        let Some(chunk) = chunk else {
            break;
        };
        for read in chunk.chunks(SSE_READ_CHUNK_SIZE) {
            framer.push(read);
            loop {
                match framer.next_event() {
                    Ok(Some(text)) => {
                        if consume_sse(
                            output,
                            request_id,
                            &text,
                            &mut collector,
                            options.interpret_responses,
                            &mut batch,
                        )
                        .await?
                        {
                            batch.flush(output).await?;
                            return Ok(None);
                        }
                    }
                    Ok(None) => break,
                    Err(error) => {
                        batch.flush(output).await?;
                        return Ok(Some(error));
                    }
                }
            }
            batch.flush(output).await?;
        }
    }
    match framer.finish() {
        Ok(Some(text)) => {
            if consume_sse(
                output,
                request_id,
                &text,
                &mut collector,
                options.interpret_responses,
                &mut batch,
            )
            .await?
            {
                batch.flush(output).await?;
                return Ok(None);
            }
        }
        Ok(None) => {}
        Err(error) => return Ok(Some(error)),
    }
    batch.flush(output).await?;
    if let Some(collector) = collector {
        emit_compact(output, request_id, collector.finish()).await?;
    }
    Ok(None)
}

async fn consume_sse(
    output: &Output,
    request_id: &str,
    text: &str,
    collector: &mut Option<CompactCollector>,
    interpret_responses: bool,
    batch: &mut EventBatch,
) -> Result<bool, std::io::Error> {
    if let Some(collector) = collector {
        if let Some(result) = collector.push(text) {
            emit_compact(output, request_id, result).await?;
            return Ok(true);
        }
    } else if interpret_responses {
        let mut event = interpret(text);
        let stream_complete = event.completes_http_stream();
        // Type metadata is not fragmented; keep it within the text budget too.
        if event
            .event_type
            .as_ref()
            .is_some_and(|kind| kind.len() > SSE_IPC_TEXT_FRAGMENT_SIZE)
        {
            event.event_type = None;
            event.python_normalization = true;
        }
        for (fragment, more) in text_fragments(&event.text, SSE_IPC_TEXT_FRAGMENT_SIZE) {
            batch
                .push(
                    output,
                    &NativeEvent::ResponsesEvent {
                        request_id: request_id.to_owned(),
                        text: fragment.to_owned(),
                        more,
                        event_type: if more { None } else { event.event_type.clone() },
                        python_normalization: !more && event.python_normalization,
                        stream_complete: !more && stream_complete,
                    },
                )
                .await?;
        }
        return Ok(stream_complete);
    } else {
        emit_sse(output, request_id, text, batch).await?;
    }
    Ok(false)
}

async fn emit_compact(
    output: &Output,
    request_id: &str,
    result: CompactResult,
) -> Result<(), std::io::Error> {
    let text = serde_json::to_string(&result)?;
    for (fragment, more) in text_fragments(&text, SSE_IPC_TEXT_FRAGMENT_SIZE) {
        emit(
            output,
            &NativeEvent::Compact {
                request_id: request_id.to_owned(),
                text: fragment.to_owned(),
                more,
            },
        )
        .await?;
    }
    Ok(())
}

async fn emit_sse(
    output: &Output,
    request_id: &str,
    text: &str,
    batch: &mut EventBatch,
) -> Result<(), std::io::Error> {
    for (fragment, more) in text_fragments(text, SSE_IPC_TEXT_FRAGMENT_SIZE) {
        batch
            .push(
                output,
                &NativeEvent::Sse {
                    request_id: request_id.to_owned(),
                    text: fragment.to_owned(),
                    more,
                },
            )
            .await?;
    }
    Ok(())
}

fn forwarded_headers(request_headers: Vec<(String, String)>) -> Result<HeaderMap, RequestError> {
    let mut headers = HeaderMap::new();
    for (name, value) in request_headers {
        let name = HeaderName::from_bytes(name.as_bytes())?;
        headers.append(name, HeaderValue::from_str(&value)?);
    }
    Ok(headers)
}

pub(crate) fn classify_error(
    error: &(dyn std::error::Error + 'static),
) -> (&'static str, &'static str, bool, bool) {
    if error.downcast_ref::<StreamIdleTimeout>().is_some() {
        return (
            "native upstream SSE body read timed out",
            "stream_idle_timeout",
            false,
            false,
        );
    }
    let Some(request_error) = error.downcast_ref::<reqwest::Error>() else {
        return ("native helper rejected the request", "setup", false, false);
    };
    let tls_verification = error_chain_has_invalid_certificate(request_error);
    if request_error.is_connect() {
        return (
            "native upstream connection failed",
            "connect",
            !tls_verification,
            tls_verification,
        );
    }
    if request_error.is_timeout() {
        return (
            "native upstream request timed out",
            "timeout",
            false,
            tls_verification,
        );
    }
    if request_error.is_body() || request_error.is_decode() {
        return (
            "native upstream response body failed",
            "body_read",
            false,
            tls_verification,
        );
    }
    (
        "native upstream request failed",
        "request",
        false,
        tls_verification,
    )
}

/// Upper bound on the diagnostic text forwarded with a transport error.
const ERROR_DETAIL_MAX_BYTES: usize = 512;

/// Describe what actually failed below a reqwest error, for diagnostics.
///
/// `classify_error` keeps the static message and phase the Python side keys
/// its handling on; this adds the causes it drops. Only typed fields of known
/// error types are rendered: the reqwest layers embed the request URL, an h2
/// GOAWAY carries the peer's opaque debug data, and an I/O or TLS error can
/// wrap arbitrary text, so none of their free-form display text is copied.
/// Causes of other types are omitted. Non-reqwest errors carry no detail.
pub(crate) fn error_detail(error: &(dyn std::error::Error + 'static)) -> Option<String> {
    let request_error = error.downcast_ref::<reqwest::Error>()?;
    source_chain_detail(std::error::Error::source(request_error))
}

fn source_chain_detail(first: Option<&(dyn std::error::Error + 'static)>) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();
    let mut current = first;
    while let Some(source) = current {
        for text in describe_cause(source) {
            if parts.last() != Some(&text) {
                parts.push(text);
            }
        }
        current = source.source();
    }
    bounded_join(&parts)
}

fn describe_cause(source: &(dyn std::error::Error + 'static)) -> Vec<String> {
    if let Some(error) = source.downcast_ref::<h2::Error>() {
        let mut texts = vec![describe_h2_error(error)];
        if let Some(io_error) = error.get_io() {
            texts.extend(describe_io_error(io_error));
        }
        return texts;
    }
    if let Some(error) = source.downcast_ref::<std::io::Error>() {
        return describe_io_error(error);
    }
    if let Some(error) = source.downcast_ref::<rustls::Error>() {
        return vec![describe_tls_error(error)];
    }
    if source.downcast_ref::<hyper::Error>().is_some() {
        // hyper renders a fixed description of its error kind.
        return vec![source.to_string()];
    }
    Vec::new()
}

fn describe_h2_error(error: &h2::Error) -> String {
    let kind = if error.is_go_away() {
        "goaway"
    } else if error.is_reset() {
        "reset"
    } else if error.is_io() {
        "io"
    } else {
        "error"
    };
    let origin = match error.is_remote() {
        true => "remote",
        false => "local",
    };
    match error.reason() {
        Some(reason) => format!("h2 {origin} {kind} {reason:?}"),
        None => format!("h2 {origin} {kind}"),
    }
}

fn describe_io_error(error: &std::io::Error) -> Vec<String> {
    let text = match error.raw_os_error() {
        Some(code) => format!("io {} (os error {code})", error.kind()),
        None => format!("io {}", error.kind()),
    };
    let mut texts = vec![text];
    let tls_error = error
        .get_ref()
        .and_then(|inner| inner.downcast_ref::<rustls::Error>());
    if let Some(tls_error) = tls_error {
        texts.push(describe_tls_error(tls_error));
    }
    texts
}

fn describe_tls_error(error: &rustls::Error) -> String {
    match error {
        rustls::Error::AlertReceived(alert) => format!("tls alert received {alert:?}"),
        rustls::Error::InvalidCertificate(_) => "tls invalid peer certificate".to_owned(),
        _ => "tls error".to_owned(),
    }
}

fn bounded_join(parts: &[String]) -> Option<String> {
    if parts.is_empty() {
        return None;
    }
    let mut detail = parts.join(": ");
    if detail.len() > ERROR_DETAIL_MAX_BYTES {
        let mut end = ERROR_DETAIL_MAX_BYTES;
        while !detail.is_char_boundary(end) {
            end -= 1;
        }
        detail.truncate(end);
    }
    Some(detail)
}

pub(crate) fn error_chain_has_invalid_certificate(
    error: &(dyn std::error::Error + 'static),
) -> bool {
    let mut current = Some(error);
    while let Some(source) = current {
        if source
            .downcast_ref::<rustls::Error>()
            .is_some_and(|error| matches!(error, rustls::Error::InvalidCertificate(_)))
        {
            return true;
        }
        current = source.source();
    }
    false
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use reqwest::header::{ACCEPT, ACCEPT_ENCODING};

    use super::{
        ClientKey, ClientPool, ERROR_DETAIL_MAX_BYTES, UNUSED_CLIENT_TTL, bounded_join,
        error_detail, forwarded_headers, source_chain_detail,
    };

    const SIGNED_URL_SENTINEL: &str = "https://storage.example/file?sig=opaque-sentinel";

    #[derive(Debug)]
    struct Layer {
        text: String,
        source: Option<Box<dyn std::error::Error + 'static>>,
    }

    impl std::fmt::Display for Layer {
        fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            formatter.write_str(&self.text)
        }
    }

    impl std::error::Error for Layer {
        fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
            self.source.as_deref()
        }
    }

    fn install_provider() {
        // Another test in this binary may have installed it already.
        let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    }

    fn account_key(pool_key: &str) -> ClientKey {
        ClientKey {
            pool_key: Some(pool_key.to_owned()),
            proxy_url: None,
            connect_timeout_ms: Some(10_000),
            decode_response: true,
        }
    }

    #[test]
    fn unused_clients_are_evicted_after_the_client_ttl() {
        install_provider();
        let mut pool = ClientPool::default();
        let account_a = account_key("account-a");
        let account_b = account_key("account-b");
        let account_c = account_key("account-c");
        let start = Instant::now();

        let in_flight = pool.get_at(&account_a, start).expect("client");
        drop(pool.get_at(&account_b, start).expect("client"));
        let after_idle = start + UNUSED_CLIENT_TTL + Duration::from_secs(5);
        drop(pool.get_at(&account_c, after_idle).expect("client"));

        assert!(
            pool.clients.contains_key(&account_a),
            "in-flight client kept"
        );
        assert!(
            !pool.clients.contains_key(&account_b),
            "idle client evicted"
        );
        assert!(pool.clients.contains_key(&account_c));

        drop(in_flight);
        let later = after_idle + UNUSED_CLIENT_TTL + Duration::from_secs(5);
        drop(pool.get_at(&account_c, later).expect("client"));

        assert!(
            !pool.clients.contains_key(&account_a),
            "released client evicted"
        );
        assert_eq!(pool.clients.len(), 1);
    }

    #[test]
    fn unused_clients_beyond_the_bound_are_evicted_least_recently_used_first() {
        install_provider();
        let mut pool = ClientPool::with_max_unused(2);
        let account_a = account_key("account-a");
        let account_b = account_key("account-b");
        let account_c = account_key("account-c");
        let account_d = account_key("account-d");
        let start = Instant::now();

        let in_flight = pool.get_at(&account_a, start).expect("client");
        drop(pool.get_at(&account_b, start).expect("client"));
        let later = start + Duration::from_secs(1);
        drop(pool.get_at(&account_c, later).expect("client"));
        let latest = later + Duration::from_secs(1);
        drop(pool.get_at(&account_d, latest).expect("client"));

        assert!(
            pool.clients.contains_key(&account_a),
            "in-flight client kept"
        );
        assert!(
            !pool.clients.contains_key(&account_b),
            "oldest unused evicted"
        );
        assert!(pool.clients.contains_key(&account_c), "newer unused kept");
        assert!(
            pool.clients.contains_key(&account_d),
            "requested client kept"
        );
        drop(in_flight);
    }

    #[test]
    fn source_chain_detail_renders_typed_causes_and_drops_free_text() {
        let error = Layer {
            text: SIGNED_URL_SENTINEL.to_owned(),
            source: Some(Box::new(std::io::Error::from_raw_os_error(104))),
        };
        let top: &(dyn std::error::Error + 'static) = &error;
        let detail = source_chain_detail(Some(top)).expect("detail");
        assert!(detail.starts_with("io "), "{detail}");
        assert!(detail.ends_with("(os error 104)"), "{detail}");
        assert!(!detail.contains("sig="), "{detail}");
        assert_eq!(source_chain_detail(None), None);
    }

    #[test]
    fn bounded_join_is_bounded_on_a_char_boundary() {
        let parts = ["\u{e9}".repeat(ERROR_DETAIL_MAX_BYTES)];
        let detail = bounded_join(&parts).expect("detail");
        assert_eq!(detail.len(), ERROR_DETAIL_MAX_BYTES);
        assert!(detail.chars().all(|character| character == '\u{e9}'));
        assert_eq!(bounded_join(&[]), None);
    }

    #[tokio::test]
    async fn goaway_debug_data_is_not_copied_into_the_detail() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        // Another test in this binary may have installed it already.
        let provider = rustls::crypto::aws_lc_rs::default_provider();
        let _ = provider.install_default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind origin");
        let address = listener.local_addr().expect("origin address");
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept client");
            let mut preface = [0_u8; 24];
            socket
                .read_exact(&mut preface)
                .await
                .expect("client preface");
            let debug_data = SIGNED_URL_SENTINEL.as_bytes();
            let mut frames = vec![0, 0, 0, 0x4, 0, 0, 0, 0, 0];
            let length = u32::try_from(8 + debug_data.len()).expect("frame length");
            frames.extend_from_slice(&length.to_be_bytes()[1..]);
            frames.extend_from_slice(&[0x7, 0, 0, 0, 0, 0]);
            frames.extend_from_slice(&0_u32.to_be_bytes());
            frames.extend_from_slice(&1_u32.to_be_bytes());
            frames.extend_from_slice(debug_data);
            socket.write_all(&frames).await.expect("write frames");
            let mut rest = Vec::new();
            let _ = socket.read_to_end(&mut rest).await;
        });

        let client = reqwest::Client::builder()
            .http2_prior_knowledge()
            .build()
            .expect("client");
        let error = client
            .get(format!("http://{address}/file"))
            .send()
            .await
            .expect_err("GOAWAY fails the request");
        let detail = error_detail(&error).expect("detail");
        assert!(detail.contains("PROTOCOL_ERROR"), "{detail}");
        assert!(!detail.contains("sig="), "{detail}");
        assert!(!detail.contains("storage.example"), "{detail}");
        server.abort();
    }

    #[test]
    fn forwarded_headers_preserve_inbound_accept_encoding() {
        let headers = forwarded_headers(vec![
            ("accept".to_owned(), "application/json".to_owned()),
            ("accept-encoding".to_owned(), "br, zstd, gzip".to_owned()),
        ])
        .expect("valid forwarded headers");

        assert_eq!(
            headers.get(ACCEPT).and_then(|value| value.to_str().ok()),
            Some("application/json")
        );
        assert_eq!(
            headers
                .get(ACCEPT_ENCODING)
                .and_then(|value| value.to_str().ok()),
            Some("br, zstd, gzip")
        );
    }
}
