// 파일 도구 — URL 바이너리 다운로드 + zip 압축. mcp_server.mjs/mcp_server_http.mjs가 공유.
// direct-telegram/tools.py의 download_file/compress_files와 같은 설계(스트리밍, 200MB 상한).
// 압축은 새 npm 의존성 없이 시스템 zip 바이너리를 그대로 씀(macOS 기본 포함).

import { createWriteStream } from "node:fs";
import { mkdir, stat, rename, unlink } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import path from "node:path";
import os from "node:os";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

const execFileAsync = promisify(execFile);
const MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024;

function expandHome(p) {
  return p && p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

export async function downloadFile(url, destPath, maxBytes) {
  const cap = Number(maxBytes) || MAX_DOWNLOAD_BYTES;
  const dest = expandHome(destPath);
  await mkdir(path.dirname(dest), { recursive: true });

  const resp = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (local-llm-mcp)" } });
  if (!resp.ok) throw new Error(`다운로드 실패: HTTP ${resp.status}`);
  const contentType = resp.headers.get("content-type") || "타입 불명";
  const declaredLen = Number(resp.headers.get("content-length") || 0);
  if (declaredLen && declaredLen > cap) {
    throw new Error(`다운로드 취소: 선언된 크기 ${(declaredLen / 1e6).toFixed(1)}MB가 한도(${(cap / 1e6).toFixed(0)}MB) 초과`);
  }

  const tmp = `${dest}.part`;
  let written = 0;
  const capped = new Transform({
    transform(chunk, _enc, cb) {
      written += chunk.length;
      if (written > cap) {
        cb(new Error(`다운로드 취소: 실제 크기가 한도(${(cap / 1e6).toFixed(0)}MB) 초과해서 중단`));
        return;
      }
      cb(null, chunk);
    },
  });

  try {
    await pipeline(Readable.fromWeb(resp.body), capped, createWriteStream(tmp));
    await rename(tmp, dest);
  } catch (err) {
    await unlink(tmp).catch(() => {});
    throw err;
  }
  return `저장 완료: ${dest} (${(written / 1e6).toFixed(2)}MB, ${contentType})`;
}

export async function compressFiles(paths, destPath) {
  if (!Array.isArray(paths) || paths.length === 0) throw new Error("압축 실패: paths가 비어있음");
  let dest = expandHome(destPath);
  if (!dest.toLowerCase().endsWith(".zip")) dest += ".zip";
  await mkdir(path.dirname(dest), { recursive: true });

  const resolved = [];
  for (const p of paths) {
    const src = expandHome(p);
    try {
      await stat(src);
    } catch {
      throw new Error(`압축 실패: 없는 경로 ${p}`);
    }
    resolved.push(src);
  }

  await unlink(dest).catch(() => {}); // 기존 zip 있으면 zip -u가 아니라 새로 만들도록 제거
  // -r: 디렉터리 재귀 / -j 없이 각 경로를 그대로(절대경로) 담되, zip이 리프 이름만 쓰도록 각 항목을
  // 개별 인자로 넘기고 cwd를 그 부모로 잡아 상대 경로로 담는다(절대경로 통째로 담기 방지).
  for (const src of resolved) {
    const cwd = path.dirname(src);
    const name = path.basename(src);
    await execFileAsync("zip", ["-r", dest, name], { cwd });
  }

  const st = await stat(dest);
  return `압축 완료: ${dest} (${(st.size / 1e6).toFixed(2)}MB)`;
}
