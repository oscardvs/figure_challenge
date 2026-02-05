
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
const LOCAL_PORT = 18080;

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
