# Using zcode-acp-server as a team-capable agent in AionUi

Notes from wiring `zcode-acp-server` into AionUi (tested on AionUi 2.2.2, macOS, arm64) so the
ZCode CLI can take part in AionUi **team mode**.

Everything below was verified against a running installation. Read
[Two independent gates](#two-independent-gates) first: the greyed-out member picker has **two**
separate causes, and fixing only one of them is not enough.

---

## Background

AionUi talks to external CLI agents over **ACP** (Agent Client Protocol). `zcode-acp-server` is a
bridge: it spawns `zcode app-server --stdio` and translates between the ZCode Protocol and ACP, so
ZCode shows up in AionUi as a **custom agent**:

```jsonc
// AionUi agent catalogue
{ "name": "ZCode", "agent_type": "acp", "agent_source": "custom",
  "agent_id": "277ba1eb", "behavior_policy": { "supports_team": false /* , ... */ } }
```

Custom agents are registered from AionUi's UI (or its `/api/agents/custom` endpoint) with a command
such as `/opt/homebrew/bin/zcode-acp-server` plus an env var pointing at the CLI:

```
ZCODE_BIN=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

Nothing in the bridge needs to change for team mode — the flags that block it live on the AionUi
side. That is the surprising part, and it is what cost most of the debugging time.

---

## Two independent gates

AionUi computes `team_selectable` for every assistant entry. An agent appears in the team
"add member" picker only when **both** gates are open.

### Gate 1 — `team_capable` (a per-agent policy flag)

`team_capable` comes from `behavior_policy`:

| agent                          | stored `behavior_policy`                                                     | `team_capable` |
| ------------------------------ | ---------------------------------------------------------------------------- | -------------- |
| builtin agents (Codex, Claude Code, …) | `{"supports_side_question": false}` (no `supports_team` key at all)  | `true`         |
| Aion CLI (internal)            | `{"supports_team": true}`                                                    | `true`         |
| **custom agents** (ZCode, …)   | `{"supports_side_question": false, "self_identity_sticky": false, "session_load_via_meta_field": false, "supports_team": false}` | **`false`** |

So the rule that matters:

> an explicit `supports_team: false` blocks team mode; **every custom agent is created with that
> exact value**, while builtins simply omit the key.

That is why a custom agent is greyed out even when its ACP handshake is perfectly healthy:

```jsonc
// GET /api/assistants -> the ZCode entry
{ "name": "ZCode", "team_selectable": false,
  "team_block_reason": "This assistant's agent does not support team mode.",
  "agent": { "type": "acp", "source": "custom" }, "agent_status": "online" }
```

Fix: flip the flag — `tools/aionui/aionui_custom_agent.py enable-team ZCode` (see below).

### Gate 2 — agent availability

Independently of the flag, an agent whose last health probe failed is also grey-listed. In that case
`team_block_reason` is the *agent's own* error string instead of the generic team message:

```jsonc
{ "name": "Kimi", "team_selectable": false, "agent_status": "offline",
  "team_block_reason": "…authentication failed…" }
{ "name": "ZCode", "team_selectable": false, "agent_status": "offline",
  "team_block_reason": "ZCode turn failed: 1113" }
```

An agent that has never been probed (`agent_status: "unchecked"`) *is* selectable, so this gate only
trips once a turn has actually failed. A successful turn clears it.

---

## Fix 1 — make the custom agent team-capable

```bash
# read-only overview of custom agents and their team flags
python3 tools/aionui/aionui_custom_agent.py list

# inspect one agent
python3 tools/aionui/aionui_custom_agent.py show ZCode

# back up the DB, then set supports_team: true for that agent
python3 tools/aionui/aionui_custom_agent.py enable-team ZCode
```

The script copies `aionui-backend.db` to a timestamped `.bak` next to the original, then updates a
single row. **Restart AionUi** afterwards — the agent catalogue is read at startup.

Verify:

```bash
curl -s "$AIONUI_BASE_URL/api/assistants" -H "Authorization: Bearer $AIONUI_RUNTIME_TOKEN" \
  | python3 -c 'import json,sys; [print(a["name"], a["team_selectable"], a.get("team_block_reason")) for a in json.load(sys.stdin)["data"]]'
```

(`AIONUI_BASE_URL` / `AIONUI_RUNTIME_TOKEN` are injected into AionUi's own helper conversations. Any
authenticated client works — the point is the `team_selectable` field.)

Revert at any time with `disable-team`, or by restoring the `.bak` file.

---

## Fix 2 — make the agent survive a real turn

With the flag flipped, the only thing left is making turns succeed. Two traps are worth knowing.

### Trap A: the default model is an OAuth "coding plan" that headless runs cannot use

The bridge's default session model is `builtin:zai-coding-plan\GLM-5.3`. Headless, ZCode serves an
**OAuth-derived project token** for that provider, and the upstream rejects turns with:

```
ZCode turn failed: 1113 — Insufficient balance or no resource package.
```

The desktop app's plan looks fine — but the desktop obtains its session through a built-in browser
flow (captcha) that a headless CLI cannot reproduce, and the plan's entitlement lives in
credential material (`~/.zcode/v2/config.json`, the "Desktop credential configuration") that a
3.12+ desktop never writes. AionUi's builtin *GLM Agent* hits the same wall for the same reason, so
this is not specific to the bridge.

Practical workarounds, in order of preference:

1. Configure a plain **API-key provider** in `~/.zcode/v2/provider_config.json` and select it as the
   session model (see Trap B). No OAuth involvement, so no 1113.
2. Obtain the manual coding-plan key and let the bridge serve it through the provider-runtime-headers
   handshake (see the `#344` discussion in `docs/TROUBLESHOOTING.md`).

### Trap B: a provider whose `baseUrl` is dead makes the turn *hang*, not fail

`~/.zcode/v2/provider_config.json` may point at an endpoint that is not running — a stale
`http://localhost:8080/v1`, for example, or a local gateway on another port. Authentication then
succeeds, so nothing errors out; the turn simply never completes and the assistant stays `offline`.

Check it and point it at a live OpenAI-compatible endpoint:

```bash
python3 - <<'EOF'
import json, os
p = os.path.expanduser("~/.zcode/v2/provider_config.json")
d = json.load(open(p))
rule = d["config"]["providerConfigRules"]["providerRules"][0]   # the active custom provider
print("baseUrl:", rule["config"]["api"].get("baseUrl"))
print("models :", rule["config"].get("personalModelIds"))
EOF
```

Verify that the chosen endpoint really answers, streaming included, before blaming the bridge:

```bash
curl -sS -N http://127.0.0.1:3425/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<your-model>","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

As a control, `tools/aionui/fake_openai_server.py` implements a local endpoint that always answers
and logs everything it receives:

```bash
python3 tools/aionui/fake_openai_server.py --port 3925 --log /tmp/fake_llm.log
# point the provider's baseUrl at http://127.0.0.1:3925/v1, then run a turn
```

* requests appear in the log → the bridge/CLI stack is healthy, the real provider is the problem
* no requests at all → the turn never reaches the model call (auth, config, or a stall)

### Trap C: Start Plan accounts — three headless blockers in a row

For a **Start Plan** account (the free quota plan; typically only `GLM-5.3-Flash` has balance),
skipping Coding Plan entirely is the cleaner route — but three things block it headless, each
with a distinct symptom:

1. **The model switch fails with `Provider Registry 中不存在 Model`.**
   A 3.12+ headless CLI builds `account:*` providers from the bundled table **plus an account
   snapshot the desktop host pushes** (`provider/updateAccountConfig`). No host, no push — every
   `account:zai-start-plan` stays `entitled: false`. The bridge's own entitlement ladder reads
   legacy `~/.zcode/v2/config.json` first, which a 3.12+ desktop never writes, so create it:

   ```jsonc
   // ~/.zcode/v2/config.json — only the models that actually have quota
   { "provider": { "builtin:zai-start-plan": {
       "name": "Start Plan", "kind": "anthropic", "enabled": true,
       "options": { "apiKey": "<plaintext JWT>",
                    "baseURL": "https://zcode.z.ai/api/v1/zcode-plan/anthropic" },
       "models": { "GLM-5.3-Flash": {} } } } }
   ```

2. **401 `Provider authentication failed` even with a valid token.**
   `zcodejwttoken` in `~/.zcode/v2/credentials.json` is an `enc:v2:os:` blob, not the JWT. Copying
   it verbatim into the config above makes the bridge serve ciphertext. Decrypt it with the
   machine key from the OS keyring (macOS):

   ```bash
   KEY=$(security find-generic-password -s "ZCode Credential Encryption" -w)
   # AES-256-GCM, key = base64url-decode(KEY); blob fingerprint = sha256(key) hex[:24]
   ```

   (Python: `pyca/cryptography` or `pycryptodome`, blob shape
   `enc:v2:os:<fp24>:<iv>.<tag>.<data>`, all base64url.) Put the **plaintext** JWT (the `eyJ…`
   string) into `config.json` `apiKey`.

3. **Turns still land on the default provider.** The bridge pins a session only when **both**
   `ZCODE_PROVIDER` and `ZCODE_MODEL` are set — one alone is ignored (`src/handlers/session.ts`,
   the `session/create` pinning block). Point the agent's command at the wrapper, which sets the
   pair before exec'ing the bridge:

   ```bash
   tools/aionui/zcode_start_plan_entry.sh
   ```

With all three in place the Start Plan gateway accepts headless turns (verified end to end with
`acp_e2e_probe.py`, `stopReason: end_turn`). Note the gateway rate-limits aggressively (~5
requests/min per IP on free plans); a team's members fan out through the same IP budget.

---

## Verifying end to end

`tools/aionui/acp_e2e_probe.py` is a dependency-free ACP client: it performs the handshake, opens a
session, optionally selects a model, sends one prompt, and prints every update plus the bridge's
stderr.

```bash
python3 tools/aionui/acp_e2e_probe.py \
  --command /opt/homebrew/bin/zcode-acp-server \
  --env ZCODE_BIN=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
  --env ZCODE_ACP_MODE=yolo \
  --model 'new-provider\some-model' \
  --log /tmp/bridge.err
```

A healthy run ends with the assistant's text and:

```
RESP session/prompt {"result": {"stopReason": "end_turn"}}
=== verdict: OK (turn completed)
```

Checklist for a custom agent to be usable in a team:

1. `acp_e2e_probe.py` → `verdict: OK`
2. `aionui_custom_agent.py show <agent>` → `team: capable`
3. AionUi restarted, `/api/assistants` shows `team_selectable: true` for the agent
4. The member picker lists it (no "该助手目前无法在团队模式中使用。")

---

## Known gaps / notes

* `interaction/requestOfficialMcpAuthHeaders` is a server→client request the bridge does not answer
  and logs as `⚠ unhandled server→client request`. Observed to be **non-fatal** — turns completed
  while it was being logged — but it is a genuine gap worth handling alongside
  `interaction/requestProviderRuntimeHeaders`.
* `credential-store: failed to read ~/.zcode/v2/config.json` is expected on a 3.12+/3.15+ desktop:
  that legacy file no longer exists, and the bridge falls back to `provider_config.json`.
* AionUi's own stock coding agents (Kimi, GLM Agent) are blocked in team mode for the same
  availability reason, not because of anything in this repository.
* Don't confuse `team_selectable` (AionUi 2.2.x) with `team_capable` (type-level field in newer
  sources): the renderer greys the member out based on `team_selectable` + `team_block_reason`.

---

## 中文摘要

在 AionUi（2.2.2）里把 ZCode CLI 通过 `zcode-acp-server` 接进来做团队开发时，会发现它在"添加成员"里
是**灰色不可选**的。原因是 Agent 目录里有**两个各自独立**的门槛：

1. **`team_capable` 标记**：AionUi 给每个**自定义 Agent** 写入的策略固定带 `supports_team: false`
   （内置 Agent 根本没这个键，所以都是可用的）。把这个键改成 `true` 即可解锁 ——
   `tools/aionui/aionui_custom_agent.py enable-team <Agent名>`（会先备份数据库，改完重启 AionUi）。
2. **Agent 可用性**：如果最近一次对话失败，接口会返回 `agent_status: offline`，同样置灰。

第 2 点的坑在 ZCode 这边：默认模型是 `builtin:zai-coding-plan\GLM-5.3`，而**无界面的 CLI 只能拿到
OAuth 派生的 project token**，上游会回 `1113 Insufficient balance`（桌面端看起来额度正常，但它依赖
浏览器验证码流程，headless 复现不了）。解决办法是给 `~/.zcode/v2/provider_config.json` 配一个
**普通 API-key 的 Provider** 并选它当模型；另外注意 `baseUrl` 如果指向一个**没在运行**的地址，表现
是"卡住不报错"，而不是报错。

验证方式：`tools/aionui/acp_e2e_probe.py` 跑一次真实对话，看到 `stopReason: end_turn` 即成功；
再用 `/api/assistants` 确认 `team_selectable: true`。

如果账号只有 **Start Plan**（免费额度，通常仅 `GLM-5.3-Flash` 有余额），见上文 "Trap C"：
headless 需要三件事齐备 —— ① 手工建 legacy `~/.zcode/v2/config.json` 声明 Start Plan provider
（桌面端 3.12+ 不再写它，entitlement 快照没有宿主推）；② credentials.json 里的 `zcodejwttoken`
是 `enc:v2:os:` 密文，要用钥匙串密钥（服务 `ZCode Credential Encryption`，AES-256-GCM）解密成
明文再填；③ 用 `tools/aionui/zcode_start_plan_entry.sh` 当启动命令，把
`ZCODE_PROVIDER`+`ZCODE_MODEL` 成对 pin 上（只设一个无效）。三者缺一的表现依次是：模型切换报
「Provider Registry 中不存在 Model」、请求 401、以及悄悄回落默认 provider 后 1113。
