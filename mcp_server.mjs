#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { buildSelfSystemPrompt } from "./self_info.mjs";
import { webSearch } from "./web_search.mjs";
import { downloadFile, compressFiles } from "./file_tools.mjs";

// 2026-08-29: 기본 채팅 모델을 LM Studio(qwen3.8-27b)에서 oMLX(Qwen3.8-Flash-Next-oQ4e-128k)로 교체.
// VISION_MODEL은 oMLX가 vision을 지원하지 않아 사실상 폐기 상태 (이미지 분석은 Claude/GPT로 직접 처리).
const LM_STUDIO_URL = process.env.LM_STUDIO_URL || "http://127.0.0.1:8766";
const LLM_API_KEY = process.env.LLM_API_KEY || "omlx";
const DEFAULT_MODEL = process.env.DEFAULT_MODEL || "Qwen3.8-Flash-Next-oQ4e-128k";
const VISION_MODEL = process.env.VISION_MODEL || "qwen/qwen3-vl-30b";

const IMAGE_MIME = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".bmp": "image/bmp",
};

// image_path(이 Mac의 파일) 또는 image_base64 중 하나를 data URI로 변환
async function imageToDataUri(args) {
  if (args.image_base64) {
    const mime = args.mime_type || "image/png";
    return `data:${mime};base64,${args.image_base64}`;
  }
  const { readFile } = await import("node:fs/promises");
  const path = await import("node:path");
  const ext = path.extname(args.image_path).toLowerCase();
  const mime = IMAGE_MIME[ext];
  if (!mime) {
    throw new Error(`지원하지 않는 이미지 형식: ${ext} (지원: ${Object.keys(IMAGE_MIME).join(", ")})`);
  }
  const buf = await readFile(args.image_path);
  return `data:${mime};base64,${buf.toString("base64")}`;
}

async function callLMStudio(model, messages, options = {}) {
  const response = await fetch(`${LM_STUDIO_URL}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${LLM_API_KEY}`,
    },
    body: JSON.stringify({
      model,
      messages,
      max_tokens: options.max_tokens || 4096,
      temperature: options.temperature ?? 0.7,
      // 2026-08-30: 안 주면 강제 사고모드가 대화가 길어질수록 응답을 극도로 느리게 만듦
      // (실측 342초→3.9초, 88배 개선 확인됨) — Hermes/boomco와 동일하게 기본 low.
      reasoning_effort: options.reasoning_effort || "low",
      stream: false,
    }),
  });

  if (!response.ok) {
    throw new Error(`LM Studio error: ${response.status} ${await response.text()}`);
  }

  const data = await response.json();
  return data.choices[0]?.message?.content || data.choices[0]?.message?.reasoning_content || "";
}

const server = new Server(
  { name: "local-llm", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "chat",
      description: "로컬 LLM(oMLX)에 질문합니다. Claude에게 묻기 전에 비용 없이 먼저 로컬 모델로 검토할 때 유용합니다.",
      inputSchema: {
        type: "object",
        properties: {
          message: { type: "string", description: "로컬 LLM에 보낼 메시지" },
          model: {
            type: "string",
            description:
              // 2026-08-30: oMLX는 모델 하나만 서빙하고 계속 교체될 예정이라 고정 enum 대신
              // 자유 입력 + 기본값은 list_models로 조회한 현재 로드 모델을 그대로 씀.
              "사용할 모델 ID (생략하면 현재 oMLX에 로드된 모델 자동 사용 — list_models로 확인 가능)",
          },
          system_prompt: {
            type: "string",
            description: "시스템 프롬프트 (선택 — 생략하면 모델이 자신의 인프라/사용법을 설명할 수 있는 기본 프롬프트가 자동 주입됨)",
          },
          temperature: { type: "number", description: "0.0~1.0, 기본 0.7" },
        },
        required: ["message"],
      },
    },
    {
      name: "list_models",
      description: "현재 oMLX에서 사용 가능한 로컬 모델 목록을 반환합니다.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "analyze_image",
      description:
        "이미지를 로컬 비전 LLM으로 분석합니다. 이미지가 외부로 나가지 않아 개인 사진/문서도 안전합니다. image_path(이 Mac의 파일 경로) 또는 image_base64 중 하나를 전달하세요.",
      inputSchema: {
        type: "object",
        properties: {
          image_path: { type: "string", description: "분석할 이미지 파일의 절대 경로 (png/jpg/webp/gif/bmp)" },
          image_base64: { type: "string", description: "base64 인코딩된 이미지 (image_path 대신 사용 가능)" },
          mime_type: { type: "string", description: "image_base64 사용 시 MIME 타입 (기본: image/png)" },
          prompt: { type: "string", description: "분석 요청 내용 (기본: 이미지 상세 설명)" },
          model: {
            type: "string",
            description: `사용할 비전 모델 (기본: ${VISION_MODEL})`,
            enum: ["qwen/qwen3-vl-30b", "qwen2.5-vl-72b-instruct"],
          },
        },
      },
    },
    {
      name: "review_code",
      description: "코드를 로컬 LLM(oMLX)으로 리뷰합니다. 보안/비용 민감한 코드를 외부로 보내지 않고 분석할 때 사용합니다.",
      inputSchema: {
        type: "object",
        properties: {
          code: { type: "string", description: "리뷰할 코드" },
          language: { type: "string", description: "프로그래밍 언어" },
          focus: {
            type: "string",
            description: "리뷰 포커스 (예: 버그, 보안, 성능, 리팩토링)",
          },
        },
        required: ["code"],
      },
    },
    {
      name: "web_search",
      description: "웹 검색(무료 백엔드, 키 불필요). 결과는 제목·URL·요약 목록.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "검색어" },
          limit: { type: "integer", description: "결과 개수(기본 5)" },
        },
        required: ["query"],
      },
    },
    {
      name: "download_file",
      description: "URL의 바이너리 파일(이미지·PDF 등)을 이 Mac의 로컬 경로에 저장합니다.",
      inputSchema: {
        type: "object",
        properties: {
          url: { type: "string" },
          path: { type: "string", description: "저장할 로컬 경로" },
          max_bytes: { type: "integer", description: "허용 최대 크기(기본 200MB)" },
        },
        required: ["url", "path"],
      },
    },
    {
      name: "compress_files",
      description: "이 Mac의 파일/디렉터리 목록을 zip 하나로 묶습니다.",
      inputSchema: {
        type: "object",
        properties: {
          paths: { type: "array", items: { type: "string" }, description: "압축할 파일·디렉터리 경로들" },
          dest: { type: "string", description: "결과 zip 경로" },
        },
        required: ["paths", "dest"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    if (name === "list_models") {
      const response = await fetch(`${LM_STUDIO_URL}/v1/models`, {
        headers: { "Authorization": `Bearer ${LLM_API_KEY}` },
      });
      const data = await response.json();
      const models = data.data.map((m) => `• ${m.id}`).join("\n");
      return { content: [{ type: "text", text: `**로드된 모델:**\n${models}` }] };
    }

    if (name === "analyze_image") {
      if (!args.image_path && !args.image_base64) {
        throw new Error("image_path 또는 image_base64 중 하나는 필수입니다");
      }
      const dataUri = await imageToDataUri(args);
      const model = args.model || VISION_MODEL;
      const reply = await callLMStudio(
        model,
        [
          {
            role: "user",
            content: [
              { type: "text", text: args.prompt || "이 이미지를 자세히 분석해서 한국어로 설명해줘. 텍스트가 있으면 그대로 읽어줘." },
              { type: "image_url", image_url: { url: dataUri } },
            ],
          },
        ],
        { temperature: 0.2 }
      );
      return { content: [{ type: "text", text: `**[${model}]**\n\n${reply}` }] };
    }

    if (name === "chat") {
      const model = args.model || DEFAULT_MODEL;
      const messages = [
        // 2026-09-02: system_prompt 생략 시 모델 스스로에 대한 정보(인프라/사용처)를
        // 기본으로 주입 — "다른 에이전트가 널 쓰는 방법이 머야?" 류 질문에 정확히 답하도록.
        { role: "system", content: args.system_prompt || buildSelfSystemPrompt(model) },
        { role: "user", content: args.message },
      ];

      const reply = await callLMStudio(model, messages, {
        temperature: args.temperature,
      });

      return {
        content: [{ type: "text", text: `**[${model}]**\n\n${reply}` }],
      };
    }

    if (name === "review_code") {
      const systemPrompt = `You are an expert code reviewer. Review code for ${args.focus || "bugs, security issues, and improvements"}. Be concise and actionable. Respond in Korean.`;
      const userMessage = `다음 ${args.language || "코드"}를 리뷰해줘:\n\n\`\`\`${args.language || ""}\n${args.code}\n\`\`\``;

      const reply = await callLMStudio(
        args.model || DEFAULT_MODEL,
        [
          { role: "system", content: systemPrompt },
          { role: "user", content: userMessage },
        ],
        { temperature: 0.3 }
      );

      return { content: [{ type: "text", text: reply }] };
    }

    if (name === "web_search") {
      const text = await webSearch(args.query, args.limit || 5);
      return { content: [{ type: "text", text }] };
    }

    if (name === "download_file") {
      const text = await downloadFile(args.url, args.path, args.max_bytes);
      return { content: [{ type: "text", text }] };
    }

    if (name === "compress_files") {
      const text = await compressFiles(args.paths, args.dest);
      return { content: [{ type: "text", text }] };
    }

    throw new Error(`Unknown tool: ${name}`);
  } catch (err) {
    return {
      content: [{ type: "text", text: `오류: ${err.message}` }],
      isError: true,
    };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
