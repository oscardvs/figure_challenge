import asyncio
import os
import subprocess
import time
from playwright.async_api import async_playwright, Page, Browser
from typing import Any

# Local proxy bridge port for environments with authenticated proxies
_BRIDGE_PORT = 18080
_bridge_process = None


def _ensure_proxy_bridge():
    """Start a local proxy bridge if an authenticated proxy is detected."""
    global _bridge_process
    if _bridge_process is not None:
        return True

    proxy_url = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                 or os.environ.get("https_proxy") or os.environ.get("http_proxy"))
    if not proxy_url:
        return False

    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    if not parsed.username:
        return False  # No auth needed, direct proxy works

    bridge_script = os.path.join(os.path.dirname(__file__), "_proxy_bridge.js")
    if not os.path.exists(bridge_script):
        # Write the bridge script
        with open(bridge_script, "w") as f:
            f.write(_PROXY_BRIDGE_JS)

    _bridge_process = subprocess.Popen(
        ["node", bridge_script],
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for bridge to start
    time.sleep(1)
    return True


_PROXY_BRIDGE_JS = r"""
const http = require('http');
const net = require('net');
const { URL } = require('url');

const UPSTREAM_PROXY = process.env.HTTPS_PROXY || process.env.HTTP_PROXY
                    || process.env.https_proxy || process.env.http_proxy;
const parsed = new URL(UPSTREAM_PROXY);
const PROXY_HOST = parsed.hostname;
const PROXY_PORT = parseInt(parsed.port);
const PROXY_USER = decodeURIComponent(parsed.username);
const PROXY_PASS = decodeURIComponent(parsed.password);
const AUTH = Buffer.from(`${PROXY_USER}:${PROXY_PASS}`).toString('base64');
const LOCAL_PORT = """ + str(_BRIDGE_PORT) + r""";

const server = http.createServer((req, res) => {
  const options = {
    hostname: PROXY_HOST, port: PROXY_PORT, path: req.url, method: req.method,
    headers: { ...req.headers, 'Proxy-Authorization': `Basic ${AUTH}` },
  };
  const proxyReq = http.request(options, (proxyRes) => {
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res);
  });
  req.pipe(proxyReq);
  proxyReq.on('error', () => { res.end(); });
});

server.on('connect', (req, clientSocket, head) => {
  const connectReq = `CONNECT ${req.url} HTTP/1.1\r\nHost: ${req.url}\r\nProxy-Authorization: Basic ${AUTH}\r\nProxy-Connection: Keep-Alive\r\n\r\n`;
  const proxySocket = net.connect(PROXY_PORT, PROXY_HOST, () => { proxySocket.write(connectReq); });
  proxySocket.once('data', (chunk) => {
    if (chunk.toString().includes('200')) {
      clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      proxySocket.write(head);
      proxySocket.pipe(clientSocket);
      clientSocket.pipe(proxySocket);
    } else {
      clientSocket.end('HTTP/1.1 502 Bad Gateway\r\n\r\n');
      proxySocket.end();
    }
  });
  proxySocket.on('error', () => { clientSocket.end(); });
  clientSocket.on('error', () => { proxySocket.end(); });
});

server.listen(LOCAL_PORT, '127.0.0.1', () => {
  console.log(`Proxy bridge on http://127.0.0.1:${LOCAL_PORT}`);
});
"""


class BrowserController:
    def __init__(self):
        self.browser: Browser | None = None
        self.context = None
        self.page: Page | None = None
        self.playwright = None

    async def start(self, url: str, headless: bool = False) -> None:
        """Launch browser and navigate to URL."""
        self.playwright = await async_playwright().start()

        launch_kwargs = {"headless": headless}
        context_kwargs = {"viewport": {"width": 1280, "height": 800}}

        # Set up proxy: use local bridge for authenticated proxies
        needs_bridge = _ensure_proxy_bridge()
        if needs_bridge:
            launch_kwargs["proxy"] = {"server": f"http://127.0.0.1:{_BRIDGE_PORT}"}
            context_kwargs["ignore_https_errors"] = True
        else:
            proxy_url = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                         or os.environ.get("https_proxy") or os.environ.get("http_proxy"))
            if proxy_url:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                launch_kwargs["proxy"] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}

        self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        self.context = await self.browser.new_context(**context_kwargs)
        self.page = await self.context.new_page()

        # Inject audio interception BEFORE page scripts run.
        # This catches SpeechSynthesis, Audio(), and blob audio from auto-play.
        await self.page.add_init_script("""
            window.__capturedSpeechTexts = [];
            window.__capturedSpeechUtterance = null;
            window.__speechDone = false;
            window.__capturedAudioSrc = null;
            window.__capturedAudio = null;
            window.__audioFullPatched = true;

            // 1. SpeechSynthesis interception
            if (window.speechSynthesis) {
                const origSpeak = window.speechSynthesis.speak.bind(window.speechSynthesis);
                window.speechSynthesis.speak = function(utterance) {
                    window.__capturedSpeechTexts.push(utterance.text);
                    window.__capturedSpeechUtterance = utterance;
                    return origSpeak(utterance);
                };
            }

            // 2. Audio constructor interception
            const OrigAudio = window.Audio;
            window.Audio = function(src) {
                const audio = new OrigAudio(src);
                window.__capturedAudioSrc = src || null;
                window.__capturedAudio = audio;
                return audio;
            };
            window.Audio.prototype = OrigAudio.prototype;

            // 3. HTMLAudioElement.play interception
            const origPlay = HTMLAudioElement.prototype.play;
            HTMLAudioElement.prototype.play = function() {
                window.__capturedAudioSrc = this.src || this.currentSrc;
                window.__capturedAudio = this;
                return origPlay.call(this);
            };

            // 4. URL.createObjectURL interception for blob audio
            const origCreateObjUrl = URL.createObjectURL;
            URL.createObjectURL = function(obj) {
                const url = origCreateObjUrl.call(URL, obj);
                if (obj instanceof Blob && (obj.type.includes('audio') || obj.type === '')) {
                    window.__capturedBlobUrl = url;
                    window.__capturedBlob = obj;
                }
                return url;
            };
        """)

        await self.page.goto(url)

    async def stop(self) -> None:
        """Close browser and proxy bridge."""
        global _bridge_process
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        if _bridge_process:
            _bridge_process.terminate()
            _bridge_process = None

    async def screenshot(self) -> bytes:
        """Take screenshot of current page."""
        return await self.page.screenshot(type="png")

    async def get_html(self) -> str:
        """Get page HTML."""
        return await self.page.content()

    async def get_url(self) -> str:
        """Get current URL."""
        return self.page.url

    async def click(self, selector: str) -> bool:
        """Click element by selector. Returns success."""
        try:
            await self.page.click(selector, timeout=2000)
            return True
        except Exception:
            return False

    async def click_by_text(self, text: str) -> bool:
        """Click element containing text."""
        try:
            await self.page.click(f"text={text}", timeout=2000)
            return True
        except Exception:
            return False

    async def type_text(self, selector: str, text: str) -> bool:
        """Type text into input field."""
        try:
            await self.page.fill(selector, text)
            return True
        except Exception:
            return False

    async def scroll_to_bottom(self) -> None:
        """Scroll to page bottom."""
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    async def close_popup_by_x(self) -> bool:
        """Try to close popup by clicking X button."""
        # Try various X button selectors
        selectors = [
            "button:has(img[alt*='close'])",
            "[aria-label*='close']",
            "[aria-label*='Close']",
            ".close-button",
            ".close",
            "button:has-text('×')",
            "button:has-text('X')",
        ]
        for sel in selectors:
            try:
                await self.page.click(sel, timeout=500)
                return True
            except Exception:
                continue
        return False

    async def wait_for_navigation(self, timeout: int = 5000) -> bool:
        """Wait for navigation to complete."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
            return True
        except Exception:
            return False

    async def execute_js(self, script: str) -> Any:
        """Execute JavaScript on page."""
        return await self.page.evaluate(script)

    async def wait_for_selector(self, selector: str, timeout: int = 5000) -> bool:
        """Wait for element to appear."""
        try:
            await self.page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            return False
