import json
import logging
import re
import threading
import tomllib
import traceback
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from iplayerdl.config_loader import get_config_path
from iplayerdl.main import run_pipeline
from iplayerdl.tracker import tracker as job_tracker

# Kept for backwards-compat tests; _replace_urls no longer relies on this regex.
URLS_ARRAY_RE = re.compile(r"(?m)^(\s*urls\s*=\s*\[)([^\]]*)(\])", re.DOTALL)

logger = logging.getLogger(__name__)


class _RunnerLogHandler(logging.Handler):
    """Forwards log records into the runner's in-memory log."""

    def __init__(self, runner: "PipelineRunner") -> None:
        super().__init__()
        self._runner = runner
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._runner.write(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never break the runner
            self.handleError(record)


class PipelineRunner:
    """Runs the pipeline in a background thread, at most one at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log: deque[str] = deque(maxlen=500)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._log.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        from iplayerdl.config_loader import apply_environment, load_config
        from iplayerdl.logging_setup import get_log_level, setup_logging

        root = logging.getLogger()
        log_handler = _RunnerLogHandler(self)
        root.addHandler(log_handler)
        try:
            config = load_config()
            apply_environment(config)
            setup_logging(get_log_level(config))
            with redirect_stdout(self), redirect_stderr(self):
                run_pipeline(config)
            self._write("Pipeline finished successfully\n")
        except Exception as e:
            self._write(f"Pipeline failed: {type(e).__name__}: {e}\n")
            self._write(traceback.format_exc() + "\n")
            logger.exception("Pipeline failed")
            job_tracker.fail_pending()
        finally:
            root.removeHandler(log_handler)

    def _write(self, text: str) -> None:
        with self._log_lock:
            for line in text.splitlines():
                self._log.append(line)

    def write(self, text: str) -> None:  # redirect_stdout target
        self._write(text)

    def flush(self) -> None:
        pass

    def status(self) -> dict:
        known = {job["url"]: dict(job) for job in job_tracker.snapshot()}
        jobs = []
        for url in _current_urls():
            job = known.pop(
                url,
                {
                    "url": url,
                    "status": "pending",
                    "percent": None,
                    "detail": "",
                    "done": 0,
                },
            )
            if job["status"] == "pending" and job_tracker.cancelled(url):
                job["status"] = "cancelled"
            jobs.append(job)
        jobs.extend(known.values())
        with self._log_lock:
            log_copy = list(self._log)
        return {
            "running": self.running,
            "log": log_copy,
            "jobs": jobs,
        }


runner = PipelineRunner()


def _read_config_text() -> str:
    return get_config_path().read_text()


def _validate_toml(text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Invalid TOML: {e}") from e


def _replace_urls(text: str, urls: list[str]) -> str:
    # TOML-aware scan: find `urls = [` then consume until matching `]`
    # respecting quoted strings (including triple-quoted) and line comments
    # so a `]` inside a URL or comment doesn't terminate early.
    header = re.search(r"(?m)^\s*urls\s*=\s*\[", text)
    if header is None:
        raise ValueError("Could not find a 'urls = [...]' array in config.toml")
    start = header.start()
    i = header.end()  # after '['
    depth = 1
    in_single = False
    in_double = False
    in_triple_single = False
    in_triple_double = False
    escaped = False
    while i < len(text) and depth > 0:
        # Handle triple-quoted string boundaries first
        if not escaped:
            if in_triple_single:
                if text[i : i + 3] == "'''":
                    in_triple_single = False
                    i += 3
                    continue
            elif in_triple_double:
                if text[i : i + 3] == '"""':
                    in_triple_double = False
                    i += 3
                    continue
            elif not in_single and not in_double:
                if text[i : i + 3] == "'''":
                    in_triple_single = True
                    i += 3
                    continue
                if text[i : i + 3] == '"""':
                    in_triple_double = True
                    i += 3
                    continue
        if in_triple_single or in_triple_double:
            i += 1
            continue
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\" and (in_single or in_double):
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "#":
                # Skip TOML line comment until newline
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
        i += 1
    if depth != 0:
        raise ValueError("Unterminated 'urls = [...]' array in config.toml")
    # header.group() contains leading whitespace + 'urls = ['; keep it verbatim
    prefix = text[start : header.end()]
    suffix = "]"
    seen: set[str] = set()
    entries = []
    for url in urls:
        url = url.strip()
        if not url or url in seen:
            continue
        escaped_url = url.replace("\\", "\\\\").replace('"', '\\"')
        entries.append(f'"{escaped_url}",')
        seen.add(url)
    body = ("\n" + "\n".join(entries) + "\n") if entries else ""
    replacement = f"{prefix}{body}{suffix}"
    return text[:start] + replacement + text[i:]


def _current_urls() -> list[str]:
    try:
        data = tomllib.loads(_read_config_text())
        return [u for u in data.get("urls", []) if isinstance(u, str)]
    except Exception:  # noqa: BLE001
        return []


def _save_config(text: str) -> None:
    path = get_config_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 5_000_000:
            raise ValueError("Invalid request body size")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def do_GET(self) -> None:
        try:
            if self.path == "/" or self.path == "/index.html":
                self._send_html(PAGE_HTML)
            elif self.path == "/api/config":
                self._send_json(
                    {
                        "path": str(get_config_path()),
                        "content": _read_config_text(),
                        "urls": _current_urls(),
                    }
                )
            elif self.path == "/api/status":
                self._send_json(runner.status())
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/config":
                data = self._read_json_body()
                content = data.get("content")
                if not isinstance(content, str):
                    raise ValueError("'content' must be a string")
                _validate_toml(content)
                _save_config(content)
                self._send_json({"ok": True})
            elif self.path == "/api/urls":
                data = self._read_json_body()
                urls = data.get("urls")
                if not isinstance(urls, list) or not all(
                    isinstance(u, str) for u in urls
                ):
                    raise ValueError("'urls' must be a list of strings")
                text = _replace_urls(_read_config_text(), urls)
                _save_config(text)
                started = runner.start() if data.get("run") else False
                self._send_json(
                    {"ok": True, "started": started, "running": runner.running}
                )
            elif self.path == "/api/cancel":
                data = self._read_json_body()
                url = data.get("url")
                if not isinstance(url, str) or not url:
                    raise ValueError("'url' must be a non-empty string")
                cancelled = job_tracker.cancel(url)
                self._send_json({"ok": True, "cancelled": cancelled})
            elif self.path == "/api/run":
                started = runner.start()
                self._send_json(
                    {"ok": True, "started": started, "running": runner.running}
                )
            else:
                self._send_json({"error": "not found"}, status=404)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)


PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iplayerDL</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, sans-serif;
    background: #12141a;
    color: #e4e6eb;
    max-width: 900px;
    margin: 0 auto;
    padding: 1.5rem;
  }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
  h1 { margin: 0; }
  h2 { margin-top: 0; font-size: 1.05rem; }
  #gear-btn {
    background: #2a2e3a;
    border: none;
    border-radius: 8px;
    width: 40px; height: 40px;
    cursor: pointer;
    display: grid; place-items: center;
    transition: transform 0.3s ease;
  }
  #gear-btn.open { transform: rotate(90deg); background: #3d6ef5; }
  #gear-btn svg { width: 22px; height: 22px; fill: #e4e6eb; }
  section {
    background: #1b1e26;
    border: 1px solid #2a2e3a;
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 1.25rem;
  }
  textarea {
    width: 100%;
    background: #12141a;
    color: #e4e6eb;
    border: 1px solid #2a2e3a;
    border-radius: 6px;
    padding: 0.6rem;
    font-family: ui-monospace, monospace;
    font-size: 0.85rem;
    resize: vertical;
  }
  #url-input { min-height: 96px; font-size: 0.95rem; font-family: inherit; }
  #config-editor { min-height: 380px; }
  .row { display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; }
  button.action {
    background: #3d6ef5;
    border: none;
    border-radius: 6px;
    color: white;
    padding: 0.5rem 1.1rem;
    font-size: 0.9rem;
    cursor: pointer;
  }
  button.secondary { background: #2a2e3a; color: #e4e6eb; border: none; border-radius: 6px; padding: 0.5rem 1.1rem; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  label.inline { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; color: #a8adb8; }
  .hint { color: #7a7f8a; font-size: 0.82rem; margin: 0.2rem 0 0.6rem; }
  #msg { font-size: 0.85rem; margin-left: auto; }
  #msg.err { color: #ff6b6b; }
  #msg.ok { color: #6bd66b; }

  .job {
    display: flex;
    align-items: center;
    gap: 0.9rem;
    padding: 0.55rem 0;
    border-bottom: 1px solid #23262f;
  }
  .job:last-child { border-bottom: none; }
  .job .name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.88rem;
  }
  .job .bar-wrap { width: 220px; height: 10px; background: #0c0d11; border-radius: 99px; overflow: hidden; flex-shrink: 0; }
  .job .bar { height: 100%; width: 0%; background: #3d6ef5; border-radius: 99px; transition: width 0.4s ease; }
  .job .bar.indeterminate {
    width: 35%;
    animation: slide 1.1s infinite linear;
    background: #f5a623;
  }
  @keyframes slide { from { margin-left: -35%; } to { margin-left: 105%; } }
  .chip {
    font-size: 0.72rem;
    padding: 0.15rem 0.55rem;
    border-radius: 99px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    width: 96px; text-align: center;
    flex-shrink: 0;
  }
  .chip.pending { background: #2a2e3a; color: #a8adb8; }
  .chip.resolving { background: #2a4a8a; color: #9db8ff; }
  .chip.downloading { background: #14532d; color: #6ee7a0; }
  .chip.transcoding { background: #5c4a10; color: #ffd66b; }
  .chip.completed { background: #14532d; color: #4ade80; }
  .chip.failed, .chip.unresolved { background: #6b1d1d; color: #ff8f8f; }
  .chip.cancelled { background: #3a3a3a; color: #b8b8b8; }
  .job .cancel-btn {
    background: #2a2e3a;
    border: none;
    border-radius: 6px;
    color: #ff8f8f;
    width: 26px; height: 26px;
    font-size: 0.85rem;
    line-height: 1;
    cursor: pointer;
    flex-shrink: 0;
    display: grid; place-items: center;
  }
  .job .cancel-btn:hover { background: #6b1d1d; color: #fff; }
  .job .detail { font-size: 0.75rem; color: #7a7f8a; min-width: 110px; text-align: right; flex-shrink: 0; }
  #log {
    background: #0c0d11;
    border: 1px solid #2a2e3a;
    border-radius: 6px;
    padding: 0.75rem;
    max-height: 300px;
    overflow-y: auto;
    font-size: 0.78rem;
    white-space: pre-wrap;
    word-break: break-word;
    min-height: 60px;
    margin-top: 0.6rem;
  }
  details summary { cursor: pointer; color: #a8adb8; font-size: 0.9rem; }
</style>
</head>
<body>
<header>
  <h1>iplayerDL <small id="config-path" style="color:#7a7f8a;font-size:0.5em;font-weight:normal"></small></h1>
  <button id="gear-btn" title="Settings">
    <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94L14.4 2.81a.47.47 0 0 0-.48-.41h-3.84a.47.47 0 0 0-.47.41L9.25 5.35c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.73 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 0 0-.12-.61l-2.01-1.58ZM12 15.6A3.61 3.61 0 0 1 8.4 12c0-1.98 1.62-3.6 3.6-3.6s3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6Z"/></svg>
  </button>
</header>

<section>
  <h2>Queue URLs</h2>
  <p class="hint">One URL per line — saving replaces the list in config.toml.</p>
  <textarea id="url-input" placeholder="https://www.bbc.co.uk/iplayer/episode/..." spellcheck="false"></textarea>
  <div class="row" style="margin-top:0.6rem">
    <button id="download-btn" class="action">Save &amp; Download</button>
    <button id="save-urls-btn" class="secondary">Save only</button>
    <span id="msg"></span>
  </div>
</section>

<section>
  <h2><span id="dot" class="status-dot" style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:0.4rem;background:#4caf50"></span>Downloads</h2>
  <div id="jobs"><p class="hint">No URLs queued.</p></div>
  <details id="log-details" style="margin-top:0.75rem">
    <summary>Pipeline output</summary>
    <pre id="log"></pre>
  </details>
</section>

<section id="settings-section" style="display:none">
  <h2>Settings</h2>
  <textarea id="config-editor" spellcheck="false"></textarea>
  <div class="row" style="margin-top:0.6rem">
    <button id="save-btn" class="action">Save settings</button>
    <button id="reload-btn" class="secondary">Reload from disk</button>
  </div>
</section>

<script>
const $ = (id) => document.getElementById(id);
let pollTimer = null;

const STATUS_LABEL = {
  pending: 'Queued', resolving: 'Resolving', downloading: 'Downloading',
  transcoding: 'Transcoding', completed: 'Done', failed: 'Failed', unresolved: 'No match',
  cancelled: 'Cancelled',
};

function showMsg(text, ok) {
  const el = $('msg');
  el.textContent = text;
  el.className = ok ? 'ok' : 'err';
  setTimeout(() => { el.textContent = ''; }, 4000);
}

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  } : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function parseUrls() {
  return $('url-input').value.split('\\n').map(s => s.trim()).filter(Boolean);
}

async function saveUrls(run) {
  const urls = parseUrls();
  if (!urls.length && run) { showMsg('No URLs entered', false); return; }
  $('download-btn').disabled = $('save-urls-btn').disabled = true;
  try {
    const data = await api('/api/urls', {urls, run});
    if (data.started) showMsg('Download started', true);
    else if (run && data.running) showMsg('Pipeline already running — URLs saved', true);
    else showMsg('URLs saved', true);
    startPolling();
  } catch (e) { showMsg(e.message, false); }
  $('download-btn').disabled = $('save-urls-btn').disabled = false;
}

async function loadConfig() {
  try {
    const data = await api('/api/config');
    $('config-editor').value = data.content;
    $('config-path').textContent = data.path;
  } catch (e) { showMsg(e.message, false); }
}

async function saveConfig() {
  $('save-btn').disabled = true;
  try {
    await api('/api/config', {content: $('config-editor').value});
    showMsg('Saved', true);
  } catch (e) { showMsg(e.message, false); }
  $('save-btn').disabled = false;
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split('/').filter(Boolean);
    if (u.hostname.includes('bbc.co.uk')
        && (parts[0] === 'episode' || parts[0] === 'episodes')) {
      const label = parts[2] || parts[1];
      if (label) return decodeURIComponent(label).replace(/-/g, ' ');
    }
    return url.replace(/^https?:\\/\\//, '');
  } catch (e) { return url; }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function renderJobs(jobs) {
  const el = $('jobs');
  if (!jobs.length) { el.innerHTML = '<p class="hint">No URLs queued.</p>'; return; }
  el.innerHTML = jobs.map(job => {
    const active = ['resolving', 'downloading', 'transcoding'].includes(job.status);
    const cancellable = active || job.status === 'pending';
    let barClass = 'bar';
    let style = '';
    if (job.percent != null && (job.status === 'downloading' || job.status === 'transcoding')) {
      style = `width:${Math.max(2, job.percent)}%`;
    } else if (active) {
      barClass += ' indeterminate';
    } else if (job.status === 'completed') {
      style = 'width:100%';
    }
    return `<div class="job">
      <span class="chip ${escHtml(job.status)}">${escHtml(STATUS_LABEL[job.status] || job.status)}</span>
      <span class="name" title="${escHtml(job.url)}">${escHtml(shortUrl(job.url))}</span>
      <div class="bar-wrap"><div class="${escHtml(barClass)}" style="${escHtml(style)}"></div></div>
      <span class="detail">${escHtml(job.detail || '')}</span>
      ${cancellable ? `<button class="cancel-btn" data-url="${encodeURIComponent(job.url)}" title="Cancel this URL">&times;</button>` : ''}
    </div>`;
  }).join('');
}

async function cancelUrl(btn) {
  btn.disabled = true;
  try {
    await api('/api/cancel', {url: decodeURIComponent(btn.dataset.url)});
    showMsg('Cancel requested', true);
  } catch (e) {
    showMsg(e.message, false);
    btn.disabled = false;
  }
}

$('jobs').addEventListener('click', (e) => {
  const btn = e.target.closest('.cancel-btn');
  if (btn) cancelUrl(btn);
});

function renderStatus(data) {
  $('dot').style.background = data.running ? '#f5a623' : '#4caf50';
  renderJobs(data.jobs || []);
  const logEl = $('log');
  logEl.textContent = data.log.length ? data.log.join('\\n') : '';
  logEl.scrollTop = logEl.scrollHeight;
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try { renderStatus(await api('/api/status')); } catch (e) {}
  }, 1500);
  api('/api/status').then(renderStatus).catch(() => {});
}

$('gear-btn').onclick = () => {
  const open = $('settings-section').style.display === 'none';
  $('settings-section').style.display = open ? '' : 'none';
  $('gear-btn').classList.toggle('open', open);
  if (open) loadConfig();
};
$('download-btn').onclick = () => saveUrls(true);
$('save-urls-btn').onclick = () => saveUrls(false);
$('save-btn').onclick = saveConfig;
$('reload-btn').onclick = loadConfig;

loadConfig().then(() =>
  api('/api/config').then(d => { $('url-input').value = (d.urls || []).join('\\n'); })
);
startPolling();
</script>
</body>
</html>"""


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    if host == "0.0.0.0":
        logger.warning(
            "Web interface bound to 0.0.0.0 — no authentication enabled. "
            "Anyone on the network can edit config and trigger downloads."
        )
    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("iplayerDL web interface listening on http://%s:%s", host, port)
    server.serve_forever()
