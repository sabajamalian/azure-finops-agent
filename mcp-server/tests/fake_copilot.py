"""Stand-in for the Copilot CLI in tests: runs a scripted session against the real FinOps MCP server.

It reads --additional-mcp-config like the real CLI, starts the azure-finops server over stdio with that
config's command and env, makes the tool calls listed in $FAKE_COPILOT_SCRIPT, then calls submit_result.

Script JSON: {"calls": [{"tool": ..., "args": {...}}], "result": {...}, "exit_code": 0}
In result strings, "{ev}" becomes the evidence ids returned so far and "{N.path.to.value:fmt}" reads a
value from the N-th call's output. "evidence_ids": "$EV" becomes the list of evidence ids.
It also records argv and environment variable names to $FAKE_COPILOT_RECORD.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REF = re.compile(r"\{(\d+)\.([\w.]+)(?::([^}]*))?\}")


def _resolve(value, outputs: list[dict], ev: list[str]):
    if value == "$EV":
        return list(ev)
    if isinstance(value, str):

        def sub(m):
            cur = outputs[int(m.group(1))]
            for part in m.group(2).split("."):
                cur = cur[int(part)] if isinstance(cur, list) else cur[part]
            return format(cur, m.group(3) or "")

        return REF.sub(sub, value).replace("{ev}", ", ".join(ev))
    if isinstance(value, dict):
        return {k: _resolve(v, outputs, ev) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, outputs, ev) for v in value]
    return value


async def session(server: dict, script: dict) -> None:
    params = StdioServerParameters(command=server["command"], args=server["args"], env=server["env"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        outputs: list[dict] = []
        ev: list[str] = []
        for call in script.get("calls", []):
            res = await s.call_tool(call["tool"], call.get("args", {}))
            out = res.structured_content or json.loads(res.content[0].text)
            out = out.get("result", out) if set(out) == {"result"} else out
            outputs.append(out)
            if out.get("evidence_id"):
                ev.append(out["evidence_id"])
        if "result" in script:
            await s.call_tool("submit_result", {"result": _resolve(script["result"], outputs, ev)})


def main(argv: list[str]) -> int:
    record = os.environ.get("FAKE_COPILOT_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as f:
            json.dump({"argv": argv, "env": sorted(os.environ)}, f)
    cfg_path = argv[argv.index("--additional-mcp-config") + 1].removeprefix("@")
    with open(cfg_path, encoding="utf-8") as f:
        server = json.load(f)["mcpServers"]["azure-finops"]
    with open(os.environ["FAKE_COPILOT_SCRIPT"], encoding="utf-8") as f:
        script = json.load(f)
    asyncio.run(session(server, script))
    return int(script.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
