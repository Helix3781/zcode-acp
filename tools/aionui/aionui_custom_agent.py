#!/usr/bin/env python3
"""Inspect — and optionally fix — AionUi's per-agent "team mode" policy.

Why this exists
---------------
AionUi stores an agent catalogue in a local SQLite database
(`~/Library/Application Support/AionUi/aionui/aionui-backend.db` on macOS).
Every custom agent is created with an explicit behaviour policy:

    {"supports_side_question": false, "self_identity_sticky": false,
     "session_load_via_meta_field": false, "supports_team": false}

`supports_team: false` makes the assistant entry report `team_capable: false`,
which the renderer surfaces as `team_selectable: false` — that is what greys the
agent out in the team "add member" picker ("该助手目前无法在团队模式中使用。"
/ "This assistant's agent does not support team mode.").

Flipping that single flag to `true` for a custom agent makes it team-capable.

Safety
------
* `list` / `show` are strictly read-only.
* `enable-team` / `disable-team` copy the database to a timestamped `.bak` file
  first, then update exactly one column of exactly one row.
* Restart AionUi after a change: the catalogue is read at startup.

Usage
-----
    python3 tools/aionui/aionui_custom_agent.py list
    python3 tools/aionui/aionui_custom_agent.py show ZCode
    python3 tools/aionui/aionui_custom_agent.py enable-team ZCode
    python3 tools/aionui/aionui_custom_agent.py disable-team ZCode

No third-party dependencies; Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

MACOS_DB = os.path.expanduser("~/Library/Application Support/AionUi/aionui/aionui-backend.db")
LINUX_DB = os.path.expanduser("~/.config/AionUi/aionui/aionui-backend.db")


def default_db() -> str:
    if sys.platform == "darwin":
        return MACOS_DB
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "AionUi", "aionui", "aionui-backend.db")
    return LINUX_DB


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["list", "show", "enable-team", "disable-team"])
    parser.add_argument("target", nargs="?", help="agent name or agent_id (required for show/enable-team/disable-team)")
    parser.add_argument("--db", default=default_db(), help="path to aionui-backend.db (default: %(default)s)")
    parser.add_argument("--all", action="store_true", help="for `list`: include builtin agents too")
    return parser.parse_args()


def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit("database not found: %s\nIs AionUi installed? Pass --db to point at another copy." % path)
    uri = "file:%s?mode=ro" % path
    return sqlite3.connect(uri, uri=True)


def rows(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.execute(
        "SELECT agent_id, name, agent_type, agent_source, behavior_policy "
        "FROM agent_metadata ORDER BY agent_source, name"
    )
    out = []
    for agent_id, name, agent_type, agent_source, policy in cursor.fetchall():
        try:
            parsed = json.loads(policy) if policy else {}
        except Exception:
            parsed = {"_raw": policy}
        out.append({"agent_id": agent_id, "name": name, "agent_type": agent_type,
                    "agent_source": agent_source, "policy": parsed})
    return out


def team_capable(entry: dict) -> bool:
    """Mirror of the backend rule: an explicit `supports_team: false` blocks team mode."""
    policy = entry["policy"]
    if policy.get("team_capable_override") is not None:
        return bool(policy["team_capable_override"])
    return policy.get("supports_team", True) is not False


def find(entries: list[dict], target: str) -> dict:
    exact = [e for e in entries if e["agent_id"] == target]
    if exact:
        return exact[0]
    named = [e for e in entries if e["name"] == target]
    if len(named) == 1:
        return named[0]
    if not named:
        sys.exit("no agent matches %r (try `list`)" % target)
    sys.exit("agent name %r is ambiguous; use one of: %s"
             % (target, ", ".join("%s (%s)" % (e["name"], e["agent_id"]) for e in named)))


def backup(path: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = "%s.bak-%s" % (path, stamp)
    source = sqlite3.connect(path)
    with sqlite3.connect(dest) as target:
        source.backup(target)  # WAL-safe copy
    source.close()
    return dest


def print_entry(entry: dict) -> None:
    print("%-22s id=%s" % (entry["name"], entry["agent_id"]))
    print("  type    : %s" % entry["agent_type"])
    print("  source  : %s" % entry["agent_source"])
    print("  policy  : %s" % json.dumps(entry["policy"], ensure_ascii=False))
    print("  team    : %s" % ("capable" if team_capable(entry) else "BLOCKED (supports_team=false)"))


def set_team(path: str, entry: dict, enabled: bool) -> None:
    policy = dict(entry["policy"])
    policy.pop("team_capable_override", None)
    policy["supports_team"] = bool(enabled)
    saved = backup(path)
    with sqlite3.connect(path) as conn:
        cursor = conn.execute("UPDATE agent_metadata SET behavior_policy = ? WHERE agent_id = ?",
                              (json.dumps(policy, ensure_ascii=False), entry["agent_id"]))
        if cursor.rowcount != 1:
            sys.exit("refusing to continue: update touched %d rows" % cursor.rowcount)
    print("database : %s" % path)
    print("backup   : %s" % saved)
    print("updated  : %s (%s) -> supports_team=%s" % (entry["name"], entry["agent_id"], enabled))
    print("next     : restart AionUi, then re-open the team member picker")


def main() -> int:
    args = parse_args()
    conn = connect(args.db)
    entries = rows(conn)

    if args.action == "list":
        shown = entries if args.all else [e for e in entries if e["agent_source"] == "custom"]
        if not shown:
            print("no custom agents found (use --all to list builtin agents)")
        for entry in shown:
            flag = "team-ok " if team_capable(entry) else "team-BLOCKED"
            print("%-12s %-22s %-10s %-10s %s"
                  % (flag, entry["name"], entry["agent_id"], entry["agent_source"], entry["agent_type"]))
        return 0

    if not args.target:
        sys.exit("`%s` needs a target (agent name or agent_id)" % args.action)
    entry = find(entries, args.target)

    if args.action == "show":
        print_entry(entry)
        return 0

    if entry["agent_type"] not in ("acp", "codex", "gemini", "antigravity"):
        print("note: agent type %r may never be team-capable regardless of this flag." % entry["agent_type"])
    set_team(args.db, entry, args.action == "enable-team")
    return 0


if __name__ == "__main__":
    sys.exit(main())
