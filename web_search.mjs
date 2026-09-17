// 무료(키 불필요) 웹 검색 — Hermes(app/plugins/web/parallel/provider.py)가 API 키 없을 때
// 쓰는 것과 같은 익명 Parallel Search MCP를 그대로 호출한다. mcp_server.mjs와
// mcp_server_http.mjs가 공유해서 쓴다(직접 tools.py 파이썬 구현과 별개, 같은 엔드포인트).

const MCP_SEARCH_URL = "https://search.parallel.ai/mcp";
const MCP_PROTOCOL_VERSION = "2025-06-18";
const MCP_CLIENT_NAME = "local-llm-mcp";
const MCP_CLIENT_VERSION = "1.0.0";

function mcpHeaders(sessionId, protocolVersion) {
  const headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": `${MCP_CLIENT_NAME}/${MCP_CLIENT_VERSION}`,
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  if (protocolVersion) headers["MCP-Protocol-Version"] = protocolVersion;
  return headers;
}

// SSE(text/event-stream) 또는 순수 JSON 응답 바디에서 JSON-RPC 메시지들을 뽑는다.
function* mcpMessages(text) {
  const body = (text || "").trim();
  if (!body) return;
  if (body[0] === "{" || body[0] === "[") {
    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch {
      return;
    }
    for (const m of Array.isArray(parsed) ? parsed : [parsed]) yield m;
    return;
  }
  let dataLines = [];
  for (const raw of body.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    } else if (!line.trim() && dataLines.length) {
      try {
        yield JSON.parse(dataLines.join("\n"));
      } catch {
        /* skip */
      }
      dataLines = [];
    }
  }
  if (dataLines.length) {
    try {
      yield JSON.parse(dataLines.join("\n"));
    } catch {
      /* skip */
    }
  }
}

function mcpEnvelope(text, requestId) {
  let fallback = {};
  for (const msg of mcpMessages(text)) {
    if (!msg || (!("result" in msg) && !("error" in msg))) continue;
    if (msg.id === requestId) return msg;
    fallback = msg;
  }
  return fallback;
}

async function mcpCall(toolName, args, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const initId = `init-${Date.now()}`;
    const initResp = await fetch(MCP_SEARCH_URL, {
      method: "POST",
      headers: mcpHeaders(null),
      signal: controller.signal,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: initId,
        method: "initialize",
        params: {
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: {},
          clientInfo: { name: MCP_CLIENT_NAME, version: MCP_CLIENT_VERSION },
        },
      }),
    });
    if (!initResp.ok) throw new Error(`MCP initialize 실패: HTTP ${initResp.status}`);
    const sessionId = initResp.headers.get("mcp-session-id");
    const initEnv = mcpEnvelope(await initResp.text(), initId);
    const negotiated = initEnv.result?.protocolVersion || MCP_PROTOCOL_VERSION;

    await fetch(MCP_SEARCH_URL, {
      method: "POST",
      headers: mcpHeaders(sessionId, negotiated),
      signal: controller.signal,
      body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    });

    const callId = `call-${Date.now()}`;
    const callResp = await fetch(MCP_SEARCH_URL, {
      method: "POST",
      headers: mcpHeaders(sessionId, negotiated),
      signal: controller.signal,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: callId,
        method: "tools/call",
        params: { name: toolName, arguments: args },
      }),
    });
    if (!callResp.ok) throw new Error(`MCP tools/call 실패: HTTP ${callResp.status}`);
    const envelope = mcpEnvelope(await callResp.text(), callId);
    if (envelope.error) throw new Error(`MCP 오류: ${JSON.stringify(envelope.error).slice(0, 300)}`);
    const result = envelope.result || {};
    if (result.isError) throw new Error(`MCP 도구 오류: ${JSON.stringify(result).slice(0, 300)}`);
    if (result.structuredContent && typeof result.structuredContent === "object") {
      return result.structuredContent;
    }
    for (const block of result.content || []) {
      if (block?.type === "text") {
        try {
          return JSON.parse(block.text);
        } catch {
          return { text: block.text };
        }
      }
    }
    return {};
  } finally {
    clearTimeout(timer);
  }
}

export async function webSearch(query, limit = 5) {
  const payload = await mcpCall("web_search", {
    objective: query,
    search_queries: [query],
    session_id: `local-llm-mcp-${Date.now()}`,
  });
  const results = (payload.results || []).slice(0, Math.max(Number(limit) || 5, 1));
  if (!results.length) return "검색 결과 없음";
  return results
    .map((r, i) => {
      const excerpt = (r.excerpts || []).join(" ").slice(0, 400) || "(요약 없음)";
      return `${i + 1}. ${r.title || "(제목 없음)"}\n   ${r.url || ""}\n   ${excerpt}`;
    })
    .join("\n");
}
