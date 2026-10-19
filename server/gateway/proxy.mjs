import fs from "fs";
import path from "path";
import https from "https";
import httpProxy from "http-proxy";
import { fileURLToPath } from "url";
import { resolveStaticRequest } from "./static-path.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.HTTPS_PORT || 8443);
const BACKEND = process.env.BACKEND_TARGET || "http://127.0.0.1:8000";
const STATIC_DIR = path.resolve(__dirname, "../web");
const TLS_KEY_FILE = process.env.TLS_KEY_FILE || path.resolve(__dirname, "../certs/dev.key");
const TLS_CERT_FILE = process.env.TLS_CERT_FILE || path.resolve(__dirname, "../certs/dev.crt");

const tls = {
  key: fs.readFileSync(TLS_KEY_FILE),
  cert: fs.readFileSync(TLS_CERT_FILE),
};

const proxy = httpProxy.createProxyServer({ target: BACKEND, changeOrigin: true, ws: true, xfwd: true });
proxy.on("error", (err, req, res) => {
  console.error("[proxy]", req?.url, err.message);
  if (res && typeof res.writeHead === "function" && !res.headersSent) {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "bad gateway" }));
  }
});

// robots.txt / sitemap.xml / llms.txt are read by crawlers, which may skip a file served as
// application/octet-stream; llms.txt also carries Chinese, hence the explicit charset.
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

const ADMIN_PUBLIC = process.env.ADMIN_PUBLIC === "true";

const server = https.createServer(tls, (req, res) => {
  const url = req.url || "/";
  // Block /admin on public gateway unless explicitly allowed
  if ((url === "/admin" || url.startsWith("/admin/")) && !ADMIN_PUBLIC) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not found" }));
    return;
  }
  if (url.startsWith("/healthz") || url.startsWith("/api/") || url.startsWith("/ws/") || url === "/admin" || url.startsWith("/admin/")) {
    proxy.web(req, res);
    return;
  }
  // 路径判断见 static-path.mjs：越界（../）一律 404，不能回落到 index.html
  const resolved = resolveStaticRequest(url, STATIC_DIR);
  if (resolved.status === 301) {
    res.writeHead(301, { Location: resolved.location, "Cache-Control": "no-cache" });
    res.end();
    return;
  }
  if (resolved.status !== 200) {
    res.writeHead(resolved.status);
    res.end(resolved.status === 400 ? "Bad Request" : "Not Found");
    return;
  }
  const fp = resolved.file;
  const ext = path.extname(fp);
  fs.readFile(fp, (err, data) => {
    if (err) { res.writeHead(404); res.end("Not Found"); return; }
    res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream", "Cache-Control": "no-cache" });
    res.end(data);
  });
});

server.on("upgrade", (req, socket, head) => {
  if ((req.url || "").startsWith("/ws/")) proxy.ws(req, socket, head);
  else socket.destroy();
});

server.listen(PORT, "0.0.0.0", () => console.log(`[gateway] https://0.0.0.0:${PORT} -> ${BACKEND}`));
