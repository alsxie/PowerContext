// 作者：FDE WorkMate
// HTTP 层跨站防护测试：真实 HTTP 服务 + 桩数据层（不依赖 seekdb）。
import assert from "node:assert/strict";
import http from "node:http";
import { after, before, test } from "node:test";
import { createApp } from "../app.mjs";
import { loadConfig } from "../config.mjs";

let server;
let port;
const calls = [];
const stub = {
  version: "stub",
  listKbs: async () => [],
  createKb: async (b) => (calls.push(b), { kb: b, created: true }),
  deleteKb: async (n) => (calls.push(n), { deleted: n }),
};

before(async () => {
  server = createApp(stub, loadConfig({}));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  port = server.address().port;
});
after(() => server.close());

function call(method, path, { headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, method, path, headers: { host: `127.0.0.1:${port}`, ...headers } }, (res) => {
      let d = "";
      res.on("data", (c) => (d += c));
      res.on("end", () => resolve({ status: res.statusCode, body: d }));
    });
    req.on("error", reject);
    if (body) req.end(body);
    else req.end();
  });
}

test("同源 JSON 写请求放行", async () => {
  const r = await call("POST", "/api/kbs", { headers: { "content-type": "application/json", origin: `http://127.0.0.1:${port}` }, body: '{"name":"a"}' });
  assert.equal(r.status, 200);
});

test("跨站 Origin 被拒绝", async () => {
  const r = await call("POST", "/api/kbs", { headers: { "content-type": "application/json", origin: "https://evil.example" }, body: "{}" });
  assert.equal(r.status, 403);
});

test("text/plain 简单请求（可被跨站表单伪造）被拒绝", async () => {
  const r = await call("POST", "/api/kbs", { headers: { "content-type": "text/plain" }, body: '{"name":"x"}' });
  assert.equal(r.status, 403);
});

test("DNS rebinding：非白名单 Host 被拒绝", async () => {
  const r = await call("GET", "/api/kbs", { headers: { host: `evil.example:${port}` } });
  assert.equal(r.status, 403);
});

test("同源 GET 正常", async () => {
  const r = await call("GET", "/api/kbs");
  assert.equal(r.status, 200);
  assert.equal(calls.filter((c) => c?.name === "x").length, 0, "被拒绝的请求不应触达数据层");
});
