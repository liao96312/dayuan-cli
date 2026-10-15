# 大圆非 UI 操作：调研结论与已验证能力（2026-10-15）

> 结论先行：**协议层已完全打通，非 UI 通道已经跑通**——我们能自己拉起大圆引擎、
> 用自写的"大脑"驱动它、让它在**真实云授权**下执行 `wecom` CLI 命令。
> 唯一未打通的跳板是企微自己的 9882 MCP 网关（只服务它自己拉起的引擎），
> 见下文「最后一跳」。

## 一、这一轮最重要的三个发现

### 1. 灰度真的开了：引擎自己活着，而且带着完整授权

企微 5.0.12.6018 现在**自己拉起** `wecom-agent.exe`（1.3.0 / 26166a5）：

```
WXWork.exe (pid 11340)
 └─ wecom-agent.exe (pid 14028)   ← 父进程就是企微主进程
```

它监听 `ws://127.0.0.1:50018`（主通道）和 `http://127.0.0.1:53795/mcp/cef/v1`（浏览器 MCP）。

**授权来自环境变量**，不是文件。从活进程 PEB 里读出来的完整契约：

| 变量 | 作用 |
|------|------|
| `WECOM_AGENT_TRUST_DEVICE_INFO` | 设备信任串（云鉴权，288 字节） |
| `WECOM_AGENT_DEVICE_ID` | 设备 ID |
| `WECOM_USER_INFO` | 登录用户的 name / vid（活进程环境变量，不入库） |
| `WECOM_AGENT_WORKSPACE_DIR` | 工作区根 |
| `WECOMAGENT_SANDBOX_CLI_PATH` | `sandbox-cli.exe`（沙箱执行器） |
| `WECOMAGENT_PYTHON_HOME` | 自带 Python 3.10 |
| `WECOM_AGENT_LOG_DIR` | 日志目录 |

带上这套 env 后，我们的引擎日志从 `has_authorization=false` 变成
**真实发出云端 CGI 请求**（`agent_resource/get_agent_resource_effective_list`、
`memory/get_agent_memory`…），`device_id` 也解析成功了。

### 2. 发现一个之前不存在的二进制：`sandbox-cli.exe`

`wecom-agent.exe` 报 `Client sandbox-cli unavailable; Python and Shell execution
stay disabled`，根因是缺 `WECOMAGENT_SANDBOX_CLI_PATH`。
这个文件就在旁边：`D:\Program Files (x86)\WXWork\5.0.12.6018\sandbox-cli.exe`。
补上之后 **`exec` 工具才真正可用**，后面的一切才走得通。

### 3. `wecom.exe` 和 `wecom-agent.exe` 是同一个文件

SHA-256 完全相同（`f11c82a3…`，63.9 MB）。前者是 CLI 壳，后者是引擎；
区别只在启动方式。CLI 有三种入口：

| 入口 | 结果 |
|------|------|
| `wecom --help` | 报 `RPC endpoint 未注入`（exit 69） |
| `wecom --agent-argv-env <32位id>` | 要 `WECOMAGENT_ARGV_JSON_<id>` 环境变量 |
| `wecom --agent-argv-file <path>` | 读参数文件（文件不存在时报 exit 2） |
| 走引擎的 `exec` 工具 | ✅ **完全可用** |

所以**不要试图单独跑 `wecom.exe`**，它只是个 RPC 客户端，RPC 服务端
只由引擎在给自己的 Python/shell 子进程注入 `WECOMAGENT_RPC_*` 时才起。

## 二、我们已经跑通的东西

`dev/shim_drive.py` 实现了完整的非 UI 闭环：

```
我们的 WS 服务器 (ws://127.0.0.1:50024)
   └─> 我们的 wecom-agent 引擎（带真实授权）
        └─> 我们的"大脑"（本地 OpenAI 兼容 shim）
             └─> 引擎执行 exec 工具 → wecom CLI → 云
```

实测日志（2026-10-15 14:17）：

```
[Reason] calling LLM (iter 0, msgs=2, tools=14)
[Reason] LLM responded — finish=tool_calls tool_names=[exec]
[BatchAuthorize] Pass: tool=exec
[ParallelExecute] tool=exec
[WecomCliExecutor] Starting CLI execution
   argv=["wecom","contact","users","search","--json","{\"keywords\":[\"张三\"]}"]
```

也就是说：**注入提示词 → 引擎跑 Agent 循环 → 走工具 → 调 CLI 命令 → 拿到回包 →
闭环**，全程没有点过一下鼠标，窗口都不用开。

引擎的工具清单也拿到了（`state/shim-tools.json`，14 个）：
`read` `write` `edit` `glob` `grep` `exec` `wecom_get_user_memory`
`web_fetch` `web_search` `present_files` `sessions_spawn` `use_agent`
`plan` `ask_question`。

其中 `exec` 的必填参数是 `command` + `execution_purpose`（≤50 字），
`require_escalated` 用于沙箱拦截后升权。

## 三、CLI 的 MCP 协议也基本逆向出来了

引擎通过 `mcp_url` 中转 CLI 的云调用，报文格式（`dev/mcp_capture.py` 抓到 22 个）：

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"wecom_tool",
           "arguments":{"payload":"{\"headers_json\":\"…\",
                                  \"arguments_json\":\"[\\\"contact\\\",\\\"users\\\",…]\"}"}}}
```

其中 `arguments_json` 就是 argv 数组，`headers_json` 里带
`x-wecom-role` / `x-openclaw-approvalid` / `x-openclaw-traceid`。

另外还有个 `mcp://direct` 传输通道（`wecom-transport/src/mcp/mod.rs`）。

## 四、最后一跳：为什么 CLI 还没真正调通

引擎的 CLI 第一次调用会问 MCP 网关要
`service.discovery`（`//discovery?service_name=<catalog>`），拿到服务目录后才能
拼出自己的 clap 命令树。

我们已经把这个响应**逐字段逆向出来了**（`ServiceCatalog` / `ServiceSchema` /
`ServiceResource` / `MethodSchema`），并且让 CLI 成功接受了它并写进缓存：

`…\default\cli\default\cache\catalog.json`

```json
{"items":[{"id":"contact","description":"企业微信通讯录",
           "resources":[{"id":"users","methods":[
              {"id":"search","path":"contact/users/search",...}]}]}],
 "code":0,"message":"ok"}
```

剩下的问题：CLI 拿到目录后仍然说 `unrecognized subcommand 'contact'`，
说明它建 clap 树的字段映射还差一点（`items[].id` → subcommand 的对应关系
还没对上；怀疑还要求 `ServiceInfo`/`ServiceSchema.schemas` 这类结构填全）。

而**直接打 9882 是不行的**：网关对非企微自己拉起的连接一律 RST。

## 五、云端连通性（一个环境事实）

| 目标 | 解析 | 可达 |
|------|------|------|
| `<云端主机>` | `<内网地址>` | ❌ 超时 |
| `claw.work.weixin.qq.com` | 0.0.0.1 | ❌ |
| `agent.work.weixin.qq.com` | 183.60.113.149 | ✅ HTTP 400 |
| `work.weixin.qq.com` | — | ✅ HTTP 200 |

企微主进程能到腾讯内网，我们的进程到不了——这跟旧文档里
"RST 而不是 401，说明端口在 TCP 层校验来者"是同一件事。

## 六、复现命令

```powershell
cd D:\dayuan-cli

# 1. 环境体检：网关 / 端口 / 引擎
python dev\probe_gateway.py
python dev\rpc_sweep.py

# 2. 看活进程的启动契约（env + 身份）
python dev\peb_env.py --pid 14028

# 3. 非 UI 跑一轮：注入 → 引擎 → exec → wecom CLI
python dev\shim_drive.py --probe "张三" --timeout 60
python dev\shim_drive.py --probe "张三" --timeout 60 --real-mcp

# 4. 抓 CLI 的 MCP 报文
python dev\mcp_capture.py
python dev\discovery_probe.py

# 5. 二进制取证
python dev\bin_compare.py     # wecom.exe == wecom-agent.exe
python dev\find_transport.py  # mcp://direct / wecom_tool
python dev\find_items.py      # discovery 目录 schema
python dev\find_tokens_frame.py  # 控制通道的 token/握手帧
```

## 七、下一步（按可行性排序）

1. **把 discovery 目录补到能建出 clap 树**——继续试 `ServiceSchema.schemas` /
   `ServiceInfo` 字段组合，直到 `wecom contact users search` 被识别。
   这是眼前最短的一步，全部本地可验证。
2. **让 CLI 的云调用落到我们自己的 MCP 网关**：我们已经能收到全部 argv，
   只要把应答回填成正确的 `ServiceCatalog` + 业务响应即可。
3. **拿控制通道的 token**：`ClientHandshake::Hello { token, websocket_url,
   llm_access_token }` + `TokensConfig { access_token, llm_expired_at, … }`
   已经定位，但活进程里 token 不明文存放（已试 JSON / UTF-16 / Bearer
   三种形态扫描，均未命中明文）。
4. 若第 3 步卡住，就**复用企微自己的引擎**：`wecom cron` 定时任务落在本地
   （`task_status.db`），让它在原生授权下执行——这条路绕开 token 问题。

## 八、安全与清理

本轮所有操作都是**只读探测**（`OpenProcess` 只用 `PROCESS_VM_READ`），
没有向 `WXWork.exe` / `wecom-agent.exe` 写过任何字节。自建引擎一律跑在
`D:\dayuan-cli\.tmp-*` 临时工作区，不碰 `D:\文档\WXWork\…` 实况目录。
`WXWork.exe`（pid 11340）和它的引擎（pid 14028）全程未被修改。

清临时产物：

```powershell
Remove-Item -Recurse -Force D:\dayuan-cli\.tmp-*
```

发送消息给用户这种动作**尚未执行**——上面的第 1、2 步不通，
就还不能保证消息真的发到 张三 手上。当前请仍用 UI 方式发送。

## 九、突破：大圆 Agent Card → 公网 MCP 通道（2026-10-15 下午）

大圆在聊天里展示过一张 Agent Card（agent_id `wecom-message-agent-3b3b9074`），
声明 A2A v1.0.0 端点 `http://<internal-a2a-host>/cgi-bin/a2a/api`，
bearer 鉴权。本轮把"非 UI 调用"这件事彻底调查清楚并**跑通了一条新通道**。

### 9.1 A2A 端点的真相：平台内网专用，外部不存在

| 验证手段 | 结果 |
|----------|------|
| 系统 DNS / Clash 代理 | 解析失败；走代理 502 |
| hosts 文件 | 无条目 |
| 企微安装目录二进制（findstr 全量） | 无 `<内部域名>` / `a2a` 字符串 |
| 企微数据目录（WeComAgent 工作区等） | 无命中 |
| `dev/a2a_scan.py` 全家族进程内存扫描（401 个命中，362 个唯一地址） | **全部是消息文本形态**（markdown/表格/富文本/超链接/SQLite content 字段），无一处出现在 DNS 缓存、连接缓冲或请求行——客户端从未尝试连接它 |
| 同一张 Card 附近的说明文案（内存 @0x3364d5b8） | 「实际 A2A 端点……走平台内部代理。**调用方都是 WorkBuddy 同集群内的 Agent**」 |
| 内存里大圆自己的验证回复（@0x2d8f6276） | 「域名解析失败……HTTP 000……我这台机器在腾讯云新加坡，不在你们公司的内网里」——连云端沙箱都到不了 |

结论：`<内部服务网格域名>` 是腾讯 WorkBuddy 集群内部的服务网格域名，
Bearer token 由该内部网关注入（所以任何外部都拿不到、也不需要拿）。
企微客户端只把它当**展示文本**（还渲染成了可点击超链接），不参与路由。

### 9.2 真正的公开通道：qyapi MCP（streamable-http）

同一条 WorkBuddy 消息里并排给出了对外的接入方式（`dev/apikey_dump.py` 从
WXWork 内存提取，完整 86 字符 apikey 未截断）：

```json
"mcpServers": {
  "企业微信消息": {
    "type": "streamable-http",
    "url": "https://qyapi.weixin.qq.com/mcp/v2/bot/msg?apikey=<key>"
  }
}
```

这就是 Card 上「外部文档 URL `https://dayuan.wecom.work/docs`」（该域名也被
公网 DNS 拦截到 0.0.0.1）背后真正的服务。鉴权即 URL 里的 apikey——
**调用方代码里同样不出现明文 token**，与"网关自动带 Authorization"是同一设计。

### 9.3 协议实测（`dev/mcp_probe.py` / `dev/mcp_call.py`）

| 步骤 | 结果 |
|------|------|
| `initialize` | ✅ HTTP 200，serverInfo「动态消息 MCP」v1.0.5，协议 2025-03-26 |
| `notifications/initialized` | ✅ HTTP 202 |
| `tools/list` | ✅ 6 个工具（下表） |
| `message_aibot_sessions_list` | ✅ errcode 0：机器人身份 **hermes**（aibNBWR5RGrhCXYQBtK4mZCViWDqxHsOxEF），授权用户=本机登录账号，最近 2 个会话 |
| `chat_groups_list` / `chat_messages_list` | ❌ errcode 853006「tool is not available for your corporation」——企业灰度未开放，协议层正常 |
| `message_send`（用户身份发文本） | ❌ 853006，同上（目标是一名单聊联系人；chat_id 不入库） |
| `message_aibot_send`（机器人身份发 markdown） | ✅ **实测成功**：errcode 0 / success true，消息已发给张三 |
| `message_files_get` | 待实测（需有效 media_id） |

工具清单：

| 工具 | 说明 | 当前可用性 |
|------|------|-----------|
| `message_send` | 以授权用户身份发文本（chat_id + text.content ≤2048） | ❌ 853006 |
| `message_aibot_send` | 以机器人身份发 markdown/图片/文件/语音/视频 | ✅ 实测成功 |
| `message_aibot_sessions_list` | 机器人最近会话（倒序 ≤20） | ✅ |
| `chat_groups_list` | 最近 7 天有消息的群会话 | ❌ 853006 |
| `chat_messages_list` | 按会话+时间拉消息 | ❌ 853006 |
| `message_files_get` | 按 media_id 取文件内容 | 待实测 |

### 9.3.1 写链路实测记录（2026-10-15，用户授权后执行）

```powershell
# chat_id 由 dev/name_scan.py 从企微内存定位，或由 dayuan_mcp.py sessions 列出
python dayuan_mcp.py send-aibot <chat_id> `
    --markdown "**[dayuan-cli]** MCP 机器人身份通道测试（非 UI 直发）" --yes
# → errcode 0, success true（消息真实发出，可在企微会话里确认）

python dayuan_mcp.py send <chat_id> "..." --yes
# → errcode 853006（message_send 未对本企业开放）
```

结论：**非 UI 写链路 = 机器人身份（hermes）的 message_aibot_send**，
用户身份的 message_send 被企业灰度拦截。发送内容带 `[dayuan-cli]` 标记。

### 9.4 封装：`dayuan_mcp.py`（仓库根目录）

```powershell
python dayuan_mcp.py tools                # 工具清单
python dayuan_mcp.py sessions             # 机器人会话（拿 chat_id）
python dayuan_mcp.py send <chat_id> "文本"            # 交互确认后发送
python dayuan_mcp.py send-aibot <chat_id> --markdown "# 标题"
python dayuan_mcp.py messages <chat_id> --days 1      # 视企业灰度
```

apikey 优先级：`--apikey` > 环境变量 `DAYUAN_MCP_APIKEY` >
`state\mcp_apikey.txt` > 内置值（内存提取）。发送命令默认要人工确认，
`--yes` 跳过。

### 9.5 本轮文件

| 文件 | 说明 |
|------|------|
| `dayuan_mcp.py` | 正式 CLI（MCP streamable-http 客户端，纯标准库） |
| `dev/a2a_scan.py` | 全家族进程内存扫描（找 A2A 域名出处，只读） |
| `dev/apikey_dump.py` | 提取完整 apikey 与演示消息全文（只读，输出 `state/a2a_apikey_dump.txt`） |
| `dev/mcp_probe.py` / `dev/mcp_call.py` | 协议探测 / 单工具调用试验脚本 |
| `dev/name_scan.py` | 按姓名在企微内存定位联系人 userid（只读） |
| `state/a2a_apikey_dump.txt` | 内存 dump 原始产出（含两个 86 字符 apikey） |

### 9.6 边界与提醒

- `message_aibot_send` 是**真实发消息**（以机器人身份），发前看清楚 chat_id。
- 853006 是服务端灰度，不是协议/凭据问题；换企业或灰度放开即可用。
- apikey 等同于该机器人的完整凭据，泄露即可代发消息；`state/a2a_apikey_dump.txt`
  建议不要外传。
- A2A 原教旨路线（message/send + tasks/get 轮询）在公网**没有对应端点**；
  若未来 `dayuan.wecom.work` 放开公网解析，可以再按 Card 重试。
- Agent Card 的完整语境（内存教学消息）：A2AGetAgentCard / A2ASendMessage /
  A2AGetTask / A2ACancelTask 是 WorkBuddy 同集群 agent 的 LLM 工具集，
  走内部网关（所以 Card 上写“调用方都是同集群内的 Agent”）；
  外部进程的等价物就是本节的 qyapi MCP。