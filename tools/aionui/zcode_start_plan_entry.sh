#!/bin/bash
# Entry wrapper for AionUi (or any ACP client) launching zcode-acp-server against
# a Start Plan account.
#
# Why this exists
# ---------------
# The bridge only pins a session's model when BOTH ZCODE_PROVIDER and
# ZCODE_MODEL are set (src/handlers/session.ts — "With both ZCODE_PROVIDER and
# ZCODE_MODEL set, the session is pinned to that pair right after create").
# Without the pin, headless sessions fall back to the account's default
# provider — for a Start-Plan-only account that is the Coding Plan endpoint,
# and every turn fails with:
#
#   ZCode turn failed: 1113 — Insufficient balance or no resource package.
#
# In addition, `account:*` providers stay unentitled in a headless CLI (the
# desktop host is what pushes `provider/updateAccountConfig`), so
# `~/.zcode/v2/config.json` must declare the Start Plan provider — see
# docs/integrations/aionui-team-mode.md ("Trap C") for the full recipe,
# including how to decrypt the `enc:v2:os:` JWT from the credential store.
#
# Usage: point the AionUi custom agent's command at this script. Both values
# can be overridden from the environment.

: "${ZCODE_PROVIDER:=account:zai-start-plan}"
: "${ZCODE_MODEL:=GLM-5.3-Flash}"
export ZCODE_PROVIDER ZCODE_MODEL

exec zcode-acp-server "$@"
