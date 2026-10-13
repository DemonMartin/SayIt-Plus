import fs from "fs";
import path from "path";

/**
 * 把请求路径解析成 web 目录下的一个文件。
 *
 * 返回值：
 *   { status: 200, file }  —— 读这个文件返回（可能是回落的 index.html）
 *   { status: 400 }        —— 路径编码非法或带 NUL
 *   { status: 404 }        —— 越界、指向目录、或回落页也不存在
 *
 * 为什么不用 new URL(...).pathname：它会提前把 "/../.env" 规范成 "/.env"，越界请求
 * 就被当成"找不到文件"、回落到 index.html 返回 200 —— 结果碰巧安全，但越界没有被识别出来。
 * 而 "/..%2f.env" 这种写法 URL 解析时不会被规范，解码后才变成 "/../.env"。
 * 所以判断一律基于原始路径、解码之后来做，而且越界直接 404，绝不进回落。
 */
export function resolveStaticRequest(rawUrl, rootDir) {
  const raw = String(rawUrl || "/").split("?")[0].split("#")[0];

  let decoded;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return { status: 400 };
  }
  if (decoded.includes("\0")) return { status: 400 };

  const rel = decoded === "/" || decoded === "" ? "index.html" : decoded;
  // 两种分隔符都切：服务器是 Linux，但解码出来的反斜杠在 Windows 上同样是分隔符
  if (rel.split(/[\\/]+/).includes("..")) return { status: 404 };

  const root = path.resolve(rootDir);
  const candidate = path.join(root, rel);
  if (!isInside(candidate, root)) return { status: 404 };

  if (!fs.existsSync(candidate)) {
    // 原有行为：找不到的路径交给前端页面（单页应用式回落）
    const fallback = path.join(root, "index.html");
    return fs.existsSync(fallback) ? { status: 200, file: fallback } : { status: 404 };
  }

  // 解开符号链接 / 目录联接后再查一次：web 里的一个链接可能指向目录外
  let real;
  let realRoot;
  try {
    real = fs.realpathSync(candidate);
    realRoot = fs.realpathSync(root);
  } catch {
    return { status: 404 };
  }
  if (!isInside(real, realRoot)) return { status: 404 };

  if (fs.statSync(real).isDirectory()) {
    // 目录只在里面有 index.html 时才当页面服务（/zh/ 是中文首页）；其余目录保持原来的 404。
    const indexFile = path.join(real, "index.html");
    if (!fs.existsSync(indexFile) || !fs.statSync(indexFile).isFile()) return { status: 404 };
    if (!raw.endsWith("/")) {
      // 补斜杠再跳，否则页面里的相对路径会按上一级解析。跳转地址由规范化后的路径段重建，
      // 不直接拼原始路径：原样拼 "//zh" 会得到 "//zh/"，浏览器会把它当成跳到别的域名。
      const segs = rel.split(/[\\/]+/).filter(Boolean).map(encodeURIComponent);
      const query = String(rawUrl || "").includes("?") ? "?" + String(rawUrl).split("?")[1].split("#")[0] : "";
      return { status: 301, location: "/" + segs.join("/") + "/" + query };
    }
    let realIndex;
    try {
      realIndex = fs.realpathSync(indexFile);
    } catch {
      return { status: 404 };
    }
    if (!isInside(realIndex, realRoot)) return { status: 404 };
    return { status: 200, file: realIndex };
  }

  return { status: 200, file: real };
}

// 用"目录 + 分隔符"作前缀，防止 /srv/web-evil 被当成 /srv/web 里面
function isInside(target, root) {
  return target === root || target.startsWith(root + path.sep);
}
