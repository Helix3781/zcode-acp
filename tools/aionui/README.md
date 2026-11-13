# AionUi integration helpers

Small, dependency-free Python 3.9+ scripts used while wiring `zcode-acp-server` into
[AionUi](https://github.com/iOfficeAI/AionUi) as a team-capable custom agent.

The full write-up is in [`docs/integrations/aionui-team-mode.md`](../../docs/integrations/aionui-team-mode.md).

| script | purpose |
| --- | --- |
| [`acp_e2e_probe.py`](./acp_e2e_probe.py) | Speak ACP to any agent command (handshake → session → one prompt) and print every update. Separates "the bridge is broken" from "the upstream provider is broken". |
| [`fake_openai_server.py`](./fake_openai_server.py) | Minimal OpenAI-compatible endpoint that always answers and logs every request. Use it as a known-good upstream while debugging a ZCode provider. |
| [`aionui_custom_agent.py`](./aionui_custom_agent.py) | Read AionUi's local agent catalogue; show and (optionally) flip the per-agent `supports_team` policy that gates team mode. Read-only unless you ask it to change something, and it backs up the database first. |
| [`zcode_start_plan_entry.sh`](./zcode_start_plan_entry.sh) | AionUi entry wrapper that pins `ZCODE_PROVIDER`/`ZCODE_MODEL` to a Start Plan model before exec'ing the bridge — without the pair, headless sessions fall back to the (unfunded) Coding Plan and die with 1113. |

Quick start:

```bash
# 1. does the agent actually complete a turn?
python3 acp_e2e_probe.py --env ZCODE_BIN=/path/to/zcode.cjs

# 2. is the custom agent allowed into teams?
python3 aionui_custom_agent.py list
python3 aionui_custom_agent.py enable-team ZCode   # backs up the DB first
```
