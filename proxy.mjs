#!/usr/bin/env node
/**
 * Anthropic API → LM Studio (OpenAI-compatible) 변환 프록시
 * Claude Code는 Anthropic 포맷으로 요청 → 이 프록시가 OpenAI 포맷으로 변환 → LM Studio 전달
 */
import express from "express";

const PORT = process.env.PORT || 4000;
// 2026-08-29: LM Studio(LAN IP:1234) 하드코딩 → oMLX(로컬:8766)로 교체.
// 프록시가 이 Mac 위에서 도니 127.0.0.1로 충분 (LAN IP는 원격 클라이언트 전용).
const LM_STUDIO_URL = process.env.LM_STUDIO_URL || "http://127.0.0.1:8766";
const LLM_API_KEY = process.env.LLM_API_KEY || "omlx";
const AUTH_HEADERS = { "Authorization": `Bearer ${LLM_API_KEY}` };

// 2026-08-29: Claude 모델명 → 특정 로컬 모델 하드매핑 제거.
// 앞으로 계속 새 모델로 교체/테스트할 예정이라, 매번 이 파일을 고치지 않도록
// 백엔드에 현재 로드된 모델을 매 요청마다 조회해서 그대로 쓴다 (짧게 캐싱).
let _modelCache = { id: null, at: 0 };
const MODEL_CACHE_TTL_MS = 30_000;

async function resolveModel() {
  const now = Date.now();
  if (_modelCache.id && now - _modelCache.at < MODEL_CACHE_TTL_MS) {
    return _modelCache.id;
  }
  const r = await fetch(`${LM_STUDIO_URL}/v1/models`, { headers: AUTH_HEADERS });
  if (!r.ok) throw new Error(`모델 목록 조회 실패: HTTP ${r.status}`);
  const data = await r.json();
  const id = data?.data?.[0]?.id;
  if (!id) throw new Error("로드된 모델이 없습니다 (oMLX/LM Studio에 모델을 올려주세요)");
  _modelCache = { id, at: now };
  return id;
}

// Anthropic messages → OpenAI messages 변환
function convertMessages(anthropicMessages, system) {
  const messages = [];
  if (system) {
    messages.push({ role: "system", content: system });
  }
  for (const msg of anthropicMessages) {
    if (typeof msg.content === "string") {
      messages.push({ role: msg.role, content: msg.content });
    } else if (Array.isArray(msg.content)) {
      const text = msg.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("\n");
      messages.push({ role: msg.role, content: text });
    }
  }
  return messages;
}

// OpenAI response → Anthropic response 변환
function convertResponse(openaiResp, model) {
  const choice = openaiResp.choices?.[0];
  const content = choice?.message?.content || choice?.message?.reasoning_content || "";
  return {
    id: openaiResp.id || `msg_${Date.now()}`,
    type: "message",
    role: "assistant",
    model,
    content: [{ type: "text", text: content }],
    stop_reason: choice?.finish_reason === "stop" ? "end_turn" : "max_tokens",
    usage: {
      input_tokens: openaiResp.usage?.prompt_tokens || 0,
      output_tokens: openaiResp.usage?.completion_tokens || 0,
    },
  };
}

// OpenAI stream chunk → Anthropic SSE 이벤트 변환
function* convertStreamChunk(chunk, msgId, model, index) {
  const delta = chunk.choices?.[0]?.delta;
  if (!delta) return;

  if (index === 0) {
    yield `event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: { id: msgId, type: "message", role: "assistant", model, content: [], usage: { input_tokens: 0, output_tokens: 0 } } })}\n\n`;
    yield `event: content_block_start\ndata: ${JSON.stringify({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } })}\n\n`;
  }

  const text = delta.content || delta.reasoning_content || "";
  if (text) {
    yield `event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text } })}\n\n`;
  }

  if (chunk.choices?.[0]?.finish_reason) {
    yield `event: content_block_stop\ndata: ${JSON.stringify({ type: "content_block_stop", index: 0 })}\n\n`;
    yield `event: message_delta\ndata: ${JSON.stringify({ type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 0 } })}\n\n`;
    yield `event: message_stop\ndata: ${JSON.stringify({ type: "message_stop" })}\n\n`;
  }
}

const app = express();
app.use(express.json({ limit: "50mb" }));

// 헬스체크
app.get("/", (req, res) => res.json({ status: "ok", lm_studio: LM_STUDIO_URL }));

// 모델 목록 (Anthropic 포맷)
app.get("/v1/models", async (req, res) => {
  try {
    const r = await fetch(`${LM_STUDIO_URL}/v1/models`, { headers: AUTH_HEADERS });
    const data = await r.json();
    res.json(data);
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

// 핵심: Anthropic /v1/messages 처리
app.post("/v1/messages", async (req, res) => {
  const { model, messages, system, max_tokens, stream, temperature } = req.body;
  let targetModel;
  try {
    targetModel = await resolveModel();
  } catch (e) {
    return res.status(502).json({ type: "error", error: { type: "api_error", message: e.message } });
  }
  const openaiMessages = convertMessages(messages, system);

  const openaiBody = {
    model: targetModel,
    messages: openaiMessages,
    max_tokens: max_tokens || 4096,
    temperature: temperature ?? 0.7,
    stream: !!stream,
  };

  console.log(`[proxy] ${model} → ${targetModel} (stream=${!!stream})`);

  try {
    const upstream = await fetch(`${LM_STUDIO_URL}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...AUTH_HEADERS },
      body: JSON.stringify(openaiBody),
    });

    if (!stream) {
      const data = await upstream.json();
      return res.json(convertResponse(data, model));
    }

    // 스트리밍 응답
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");

    const msgId = `msg_${Date.now()}`;
    let chunkIndex = 0;
    let buffer = "";

    for await (const raw of upstream.body) {
      buffer += new TextDecoder().decode(raw);
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") continue;
        try {
          const chunk = JSON.parse(payload);
          for (const event of convertStreamChunk(chunk, msgId, model, chunkIndex++)) {
            res.write(event);
          }
        } catch {}
      }
    }
    res.end();
  } catch (e) {
    console.error("[proxy] error:", e.message);
    res.status(502).json({ type: "error", error: { type: "api_error", message: e.message } });
  }
});

app.listen(PORT, () => {
  console.log(`\n✓ Anthropic→LM Studio 프록시 실행 중`);
  console.log(`  Listen  : http://localhost:${PORT}`);
  console.log(`  LM Studio: ${LM_STUDIO_URL}`);
  console.log(`\n  Claude Code 사용법:`);
  console.log(`  export ANTHROPIC_BASE_URL=http://localhost:${PORT}`);
  console.log(`  export ANTHROPIC_API_KEY=local\n`);
});
