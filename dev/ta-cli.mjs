/**
 * ta-cli.mjs — 内核子代理最小 CLI（自己实现签名，不依赖 pi 扩展）
 *
 * 依据 docs/pi-subagent.md：
 *   POST /session                     建会话
 *   POST /session/:id/message         发任务（同步阻塞，跑完才返回）
 *   GET  /session/:id/message         取历史（断线后捞结果）
 *   POST /session/:id/abort           终止（治孤儿的关键，见 §5）
 *   GET  /permission  /  POST /permission/:id/reply
 *   GET  /config      /  PATCH /global/config
 *
 * 用 node:http 裸请求而不是 fetch：内核 message 接口同步阻塞，
 * undici 默认 headersTimeout=300s 会把长任务误判成 fetch failed（§3.2）。
 */
import { createHmac, randomBytes } from "node:crypto";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const KERNEL = process.env.TA_KERNEL_URL || "http://127.0.0.1:4397";
// 仓库根：脚本可能在 teleagent-proxy/scripts/pi 下，也可能被放到别处。
// 用 TA_REPO 显式指定；否则按候选顺序找那个真的带 scripts/session.key 的。
const REPO_CANDIDATES = [
	process.env.TA_REPO,
	path.join(HERE, ".."),
	path.join(HERE, "..", ".."),
	path.join(HERE, "..", "..", ".."),
	"D:\\tele proxy\\teleagent-proxy",
].filter(Boolean);
const REPO = REPO_CANDIDATES.find((c) => fs.existsSync(path.join(c, "scripts", "session.key")))
	|| REPO_CANDIDATES[0];
const KEY_FILE = process.env.TA_SESSION_KEY_FILE || path.join(REPO, "scripts", "session.key");
const PROVIDER = process.env.TA_PROVIDER_ID || "NewApi";
const PERMISSION_MODE = process.env.TA_PERMISSION_MODE || "yolo";
const AGENT = process.env.TA_AGENT === "" ? null : (process.env.TA_AGENT || "ta-subagent");
const GET_KEY = process.env.TA_GET_SESSION_KEY || path.join(REPO, "scripts", "get-session-key.py");

function readKey() {
	if (process.env.TA_SESSION_KEY) return process.env.TA_SESSION_KEY.trim();
	try {
		const k = fs.readFileSync(KEY_FILE, "utf8").trim();
		return k || null;
	} catch {
		return null;
	}
}

async function refreshKey() {
	// sessionKey 每次 App 重启都会变 → 401 时重扫内存
	const { execFileSync } = await import("node:child_process");
	try {
		execFileSync("python", [GET_KEY], { stdio: "ignore" });
	} catch {
		return null;
	}
	return readKey();
}

function sign(method, pathWithQuery, key) {
	const ts = String(Date.now());
	const nonce = randomBytes(12).toString("hex");
	const payload = ["local-v1", method.toUpperCase(), pathWithQuery, ts, nonce].join("\n");
	const sig = createHmac("sha256", key).update(payload).digest("base64url");
	return {
		"X-SA-Sign-Version": "local-v1",
		"X-SA-Timestamp": ts,
		"X-SA-Nonce": nonce,
		"X-SA-Signature": sig,
	};
}

function rawRequest(url, method, headers, body, timeoutMs) {
	return new Promise((resolve, reject) => {
		const u = new URL(url);
		let settled = false;
		let timer;
		const req = http.request(
			{ protocol: u.protocol, hostname: u.hostname, port: u.port || 80,
			  path: u.pathname + u.search, method, headers },
			(res) => {
				const chunks = [];
				res.on("data", (c) => chunks.push(c));
				res.on("end", () => {
					if (settled) return;
					settled = true;
					clearTimeout(timer);
					resolve({ status: res.statusCode ?? 0, text: Buffer.concat(chunks).toString("utf8") });
				});
			},
		);
		// 总时限，不是空闲超时：任务正常跑着就不会被掐
		timer = setTimeout(() => req.destroy(new Error(`超过总时限 ${Math.round(timeoutMs / 1000)}s`)), timeoutMs);
		req.on("error", (e) => { if (!settled) { settled = true; clearTimeout(timer); reject(e); } });
		if (body !== undefined) req.write(body);
		req.end();
	});
}

let keyCache = readKey();

async function call(method, p, body, timeoutMs = 60_000) {
	let key = keyCache;
	if (!key) { key = await refreshKey(); keyCache = key; }
	if (!key) throw new Error("拿不到 sessionKey：确认 TeleAgent 在运行，或设 TA_SESSION_KEY");
	const payload = body === undefined ? undefined : JSON.stringify(body);
	const headers = { ...sign(method, p, key) };
	if (payload !== undefined) {
		headers["Content-Type"] = "application/json";
		headers["Content-Length"] = String(Buffer.byteLength(payload));
	}
	let res = await rawRequest(KERNEL + p, method, headers, payload, timeoutMs);
	if (res.status === 401) {
		key = await refreshKey();
		if (key) {
			keyCache = key;
			const h2 = { ...sign(method, p, key) };
			if (payload !== undefined) {
				h2["Content-Type"] = "application/json";
				h2["Content-Length"] = String(Buffer.byteLength(payload));
			}
			res = await rawRequest(KERNEL + p, method, h2, payload, timeoutMs);
		}
	}
	let json = null;
	try { json = JSON.parse(res.text); } catch { /* 非 JSON */ }
	return { status: res.status, text: res.text, json };
}

// ------------------------------------------------------------------ 命令 ----
async function cmdConfig() {
	const r = await call("GET", "/config");
	if (r.status !== 200) return console.log("HTTP", r.status, r.text.slice(0, 300));
	const cfg = r.json;
	console.log("small_model :", cfg.small_model);
	console.log("providers   :", Object.keys(cfg.provider || {}).join(", "));
	console.log("agents      :", Object.keys(cfg.agent || {}).join(", "));
	const ta = cfg.agent?.["ta-subagent"];
	if (ta) {
		console.log("ta-subagent : prompt", (ta.prompt || "").length, "chars | mode", ta.mode,
			"| permission", Object.keys(ta.permission || {}).join(","));
	} else {
		console.log("ta-subagent : 未注册");
	}
}

async function cmdRun(text, { model = "chat-flash", timeoutSec = 300, agent = AGENT } = {}) {
	const created = await call("POST", "/session", { title: text.slice(0, 40) }, 20_000);
	if (created.status !== 200) throw new Error(`建会话失败 HTTP ${created.status}: ${created.text.slice(0, 200)}`);
	const id = created.json.id;
	console.log(`[session] ${id}`);
	const body = {
		model: { providerID: PROVIDER, modelID: model },
		parts: [{ type: "text", text }],
		mode: PERMISSION_MODE,
	};
	if (agent) body.agent = agent;
	const t0 = Date.now();
	try {
		const r = await call("POST", `/session/${id}/message`, body, timeoutSec * 1000);
		console.log(`[done] HTTP ${r.status} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
		if (r.status !== 200) { console.log(r.text.slice(0, 800)); return id; }
		// POST 只返回**最后一条** assistant 消息；工具轨迹在之前的轮次里，
		// 所以拉一次完整历史，否则会误判成"没调用工具"（实测踩过）。
		const hist = await call("GET", `/session/${id}/message`, undefined, 20_000);
		printParts(hist.json ?? r.json);
		return id;
	} catch (e) {
		// 超时 ≠ 失败：先捞历史，捞不到再 abort（§5.3）
		console.log(`[timeout] ${e.message} — 尝试从历史捞结果`);
		const got = await recover(id, timeoutSec);
		if (got) printParts(got);
		else { console.log("[abort] 捞不回来，主动终止以免留孤儿"); await call("POST", `/session/${id}/abort`, {}); }
		return id;
	}
}

async function recover(id, timeoutSec) {
	const deadline = Date.now() + timeoutSec * 1000;
	let prev = null, stable = 0;
	while (Date.now() < deadline) {
		await new Promise((r) => setTimeout(r, 3000));
		const r = await call("GET", `/session/${id}/message`, undefined, 20_000);
		const txt = r.text || "";
		if (txt === prev) { stable++; if (stable >= 2) return r.json; } else stable = 0;
		prev = txt;
	}
	return null;
}

function describeTool(p) {
	const name = p.tool || p.name || "?";
	const st = p.state?.status || "";
	const meta = p.state?.metadata || {};
	const exit = meta.exit !== undefined ? ` exit=${meta.exit}` : "";
	const input = p.state?.input || {};
	const arg = input.command || input.filePath || input.path || input.pattern || input.query || "";
	const argStr = arg ? ` ${String(arg).slice(0, 90)}` : "";
	return { name, st, exit, argStr };
}

function printParts(json, { verbose = true } = {}) {
	// POST /session/:id/message 返回 { data: { info, parts } }
	// GET  /session/:id/message 返回 [{ info, parts }, ...]（多轮 = 多条 message）
	let messages = [];
	if (json?.data?.parts) messages = [{ info: json.data.info, parts: json.data.parts }];
	else if (Array.isArray(json)) messages = json;
	else if (json?.parts) messages = [{ info: json.info, parts: json.parts }];

	if (!messages.length) { console.log("(没有消息)", JSON.stringify(json).slice(0, 300)); return; }

	const tools = [];
	let lastText = null;
	for (const m of messages) {
		for (const p of m.parts || []) {
			if (p.type === "text" && p.text) lastText = p.text;
			else if (p.type === "tool") tools.push(describeTool(p));
		}
	}
	if (verbose) {
		for (const t of tools) {
			console.log(`[tool] ${t.name}${t.st ? " " + t.st : ""}${t.exit}${t.argStr}`);
		}
	}
	if (lastText) console.log(lastText);

	const info = messages[messages.length - 1]?.info;
	if (info?.tokens) {
		const t = info.tokens;
		console.log(`\n[tokens] input=${t.input} output=${t.output} total=${t.total}` +
			(t.cache ? ` cacheRead=${t.cache.read}` : "") +
			` | finish=${info.finish || "?"} | messages=${messages.length} | tools=${tools.length}`);
	}
}

async function cmdSessions() {
	const r = await call("GET", "/session");
	const list = Array.isArray(r.json) ? r.json : [];
	console.log(`共 ${list.length} 个会话`);
	for (const s of list.slice(0, 30)) {
		const t = s.time?.updated ? new Date(s.time.updated).toLocaleString("zh-CN") : "";
		console.log(`  ${s.id}  ${t}  ${(s.title || "").slice(0, 44)}`);
	}
}

async function cmdAbort(id) { const r = await call("POST", `/session/${id}/abort`, {}); console.log("abort", r.status, r.text.slice(0, 120)); }
async function cmdPerm() {
	const r = await call("GET", "/permission");
	console.log(r.status, JSON.stringify(r.json).slice(0, 600));
}

async function main() {
	const [cmd, ...rest] = process.argv.slice(2);
	switch (cmd) {
		case "config": return cmdConfig();
		case "sessions": return cmdSessions();
		case "permission": return cmdPerm();
		case "abort": return cmdAbort(rest[0]);
		case "run": {
			const opts = {};
			let text = null;
			for (let i = 0; i < rest.length; i++) {
				if (rest[i] === "--model") opts.model = rest[++i];
				else if (rest[i] === "--timeout") opts.timeoutSec = Number(rest[++i]);
				else if (rest[i] === "--no-agent") opts.agent = null;
				else if (rest[i] === "--task-file") {
					// 任务里含 {} " $ 等字符时，走命令行会被 shell 吃掉（实测被截断成 "{"）。
					// 复杂任务一律用文件传。
					const f = rest[++i];
					text = fs.readFileSync(f, "utf8");
				} else if (text === null) text = rest[i];
			}
			if (text === null) { console.log("缺少任务文本（或 --task-file <路径>）"); process.exit(2); }
			return cmdRun(text, opts);
		}
		default:
			console.log("用法: node ta-cli.mjs <config|sessions|permission|abort ID|run '任务' [--task-file f] [--model chat-flash] [--timeout 300] [--no-agent]>");
			console.log("  含 {} \" $ 的任务请用 --task-file，避免被 shell 截断");
	}
}
main().catch((e) => { console.error("ERR", e.message); process.exit(1); });
