// 作者：FDE WorkMate
// 入口：打开 seekdb（嵌入式）→ 启动 HTTP 服务；退出时关闭数据库，保证数据与索引落盘。
import { createApp } from "./app.mjs";
import { loadConfig } from "./config.mjs";
import { KnowledgeBaseService } from "./kb.mjs";

const config = loadConfig();
const service = await new KnowledgeBaseService(config).open();
const server = createApp(service, config);

server.listen(config.port, config.host, () => {
  console.log(`[edge-kb] seekdb ${service.version} @ ${config.dbPath} (database=${config.database})`);
  console.log(`[edge-kb] http://${config.host}:${config.port}/`);
  if (config.host !== "127.0.0.1" && config.host !== "localhost") {
    console.warn("[edge-kb] 警告：API 无鉴权，仅建议在本机回环地址上监听");
  }
});

let closing = false;
async function shutdown(sig) {
  if (closing) return;
  closing = true;
  console.log(`[edge-kb] ${sig}，关闭中…`);
  server.close();
  await service.close().catch((e) => console.error(e));
  process.exit(0);
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
