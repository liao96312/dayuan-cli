/**
 * ta-fan.mjs — 扇出并发（docs/pi-subagent.md §7.2）
 *
 * 关键点：
 *  - **不要**发 N 次调用、也**不要**塞进一个 task 让子代理串行做；一次调用用 tasks 扇出。
 *  - 内核实测 48 并发无压力（§4），真正的墙是云网关限流。
 *  - 一个 task 只做一件事；多件事会让子代理进入"自查重做"死循环（§3.3）。
 */
import { createHmac, randomBytes } from "node:crypto";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const KERNEL = process.env.TA_KERNEL_URL || "http://127.0.0.1:4397";
const REPO_CANDIDATES = [
	process.env.TA_REPO,
	path.join(HERE, ".."),
	path.join(HERE, "..", ".."),
	"D:\\tele proxy\\teleagent-proxy",
].filter(Boolean);
const REPO = REPO_CANDIDATES.find((c) => fs.existsSync(path.join(c, "scripts", "session.key")))
	|| REPO_CANDIDATES[0];
const KEY_FILE = process.env.TA_SESSION_KEY_FILE || path.join(REPO, "scripts", "session.key");
const PROVIDER = process.env.TA_PROVIDER_ID || "NewApi";
const AGENT = process.env.TA_AGENT === "" ? null : (process.env.TA_AGENT || "ta-subagent");
const MODE = process.env.TA_PERMISSION_MODE || "yolo";

function key() {
	if (process.env.TA_SESSION_KEY) return process.env.TA_SESSION_KEY.trim();
	return fs.readFileSync(KEY_FILE, "utf8").trim();
}

function sign(method, p, k) {
	const ts = String(Date.now());
	const nonce = randomBytes(12).toString("hex");
	const sig = createHmac("sha256", k).update(["local-v1", method, p, ts, nonce].join("\n")).digest("base64url");
	return { "X-SA-Sign-Version": "local-v1", "X-SA-Timestamp": ts, "X-SA-Nonce": nonce, "X-SA-Signature": sig };
}

function raw(method, p, body, timeoutMs) {
	return new Promise((resolve, reject) => {
		const data = body === undefined ? undefined : JSON.stringify(body);
		const h = { ...sign(method, p, key()) };
		if (data) { h["Content-Type"] = "application/json"; h["Content-Length"] = Buffer.byteLength(data); }
		let settled = false;
		const req = http.request({ hostname: "127.0.0.1", port: 4397, path: p, method, headers: h }, (r) => {
			const c = [];
			r.on("data", (d) => c.push(d));
			r.on("end", () => { if (!settled) { settled = true; clearTimeout(tm); resolve({ status: r.statusCode, text: Buffer.concat(c).toString("utf8") }); } });
		});
		const tm = setTimeout(() => req.destroy(new Error(`超过总时限 ${Math.round(timeoutMs / 1000)}s`)), timeoutMs);
		req.on("error", (e) => { if (!settled) { settled = true; clearTimeout(tm); reject(e); } });
		if (data) req.write(data);
		req.end();
	});
}

async function call(method, p, body, timeoutMs = 60_000) {
	const r = await raw(method, p, body, timeoutMs);
	let json = null;
	try { json = JSON.parse(r.text); } catch { /* 非 JSON */ }
	return { status: r.status, text: r.text, json };
}

function summarize(hist) {
	const messages = Array.isArray(hist) ? hist : [];
	const tools = [];
	let text = null;
	for (const m of messages) {
		for (const p of m.parts || []) {
			if (p.type === "text" && p.text) text = p.text;
			else if (p.type === "tool") tools.push({ name: p.tool, status: p.state?.status, exit: p.state?.metadata?.exit });
		}
	}
	return { tools, text, messages: messages.length };
}

async function runOne(task, model, timeoutSec, idx) {
	const created = await call("POST", "/session", { title: `fan#${idx} ${task.slice(0, 30)}` }, 20_000);
	const id = created.json?.id;
	if (!id) return { idx, ok: false, error: `建会话失败 HTTP ${created.status}` };
	const body = {
		model: { providerID: PROVIDER, modelID: model },
		parts: [{ type: "text", text: task }],
		mode: MODE,
	};
	if (AGENT) body.agent = AGENT;
	const t0 = Date.now();
	try {
		const r = await call("POST", `/session/${id}/message`, body, timeoutSec * 1000);
		const hist = await call("GET", `/session/${id}/message`, undefined, 20_000);
		const s = summarize(hist.json);
		return { idx, ok: r.status === 200, sessionId: id, ms: Date.now() - t0, ...s };
	} catch (e) {
		// 超时 ≠ 失败：捞历史；捞不回再 abort（§5.3）
		const hist = await call("GET", `/session/${id}/message`, undefined, 20_000).catch(() => null);
		const s = summarize(hist?.json);
		if (s.text) return { idx, ok: true, recovered: true, sessionId: id, ms: Date.now() - t0, ...s };
		await call("POST", `/session/${id}/abort`, {}).catch(() => {});
		return { idx, ok: false, sessionId: id, error: e.message, aborted: true };
	}
}

async function main() {
	const args = process.argv.slice(2);
	const opts = { model: "chat-flash", timeoutSec: 300, concurrency: 0 };
	let tasksFile = null, inlineTasks = [];
	for (let i = 0; i < args.length; i++) {
		if (args[i] === "--model") opts.model = args[++i];
		else if (args[i] === "--timeout") opts.timeoutSec = Number(args[++i]);
		else if (args[i] === "--concurrency") opts.concurrency = Number(args[++i]);
		else if (args[i] === "--tasks-file") tasksFile = args[++i];
		else inlineTasks.push(args[i]);
	}
	let tasks = inlineTasks;
	if (tasksFile) {
		// 一行一个任务（含 {} " $ 的任务必须走文件，否则被 shell 截断）
		tasks = fs.readFileSync(tasksFile, "utf8").split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));
	}
	if (!tasks.length) { console.log("用法: node ta-fan.mjs --tasks-file tasks.txt [--concurrency 8] [--model chat-flash] [--timeout 900]"); process.exit(2); }

	const limit = opts.concurrency > 0 ? opts.concurrency : tasks.length;
	console.log(`[fan] ${tasks.length} 个任务 | 并发 ${limit} | model ${opts.model} | agent ${AGENT || "(无)"}`);
	const t0 = Date.now();
	const results = new Array(tasks.length);
	let next = 0;
	// 观察真实并发重叠
	let active = 0, maxActive = 0;
	async function worker() {
		while (true) {
			const i = next++;
			if (i >= tasks.length) return;
			active++; maxActive = Math.max(maxActive, active);
			try { results[i] = await runOne(tasks[i], opts.model, opts.timeoutSec, i); }
			catch (e) { results[i] = { idx: i, ok: false, error: e.message }; }
			active--;
		}
	}
	await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
	const wall = Date.now() - t0;

	const sum = results.reduce((a, r) => a + (r.ms || 0), 0);
	console.log(`\n[fan] 墙钟 ${(wall / 1000).toFixed(1)}s | 各任务耗时之和 ${(sum / 1000).toFixed(1)}s | 观测最大并发 ${maxActive}`);
	for (const r of results) {
		const tag = r.ok ? "OK  " : "FAIL";
		console.log(`  [${tag}] #${r.idx} ${(r.ms / 1000).toFixed(1)}s tools=${r.tools?.length ?? 0} ` +
			`${r.recovered ? "(从历史捞回)" : ""}${r.aborted ? "(已 abort)" : ""} ${r.error || ""}`);
		if (r.text) console.log(`         ${r.text.split("\n").filter(Boolean).slice(-2).join(" | ").slice(0, 160)}`);
	}
	const okCount = results.filter((r) => r.ok).length;
	console.log(`\n[fan] 成功 ${okCount}/${tasks.length}`);
	process.exit(okCount === tasks.length ? 0 : 1);
}

main().catch((e) => { console.error("ERR", e.message); process.exit(1); });
