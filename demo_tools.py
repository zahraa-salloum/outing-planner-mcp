"""Call each MCP tool directly (no LLM) against the live Open-Meteo API, including failure cases.

The server is started as a subprocess over stdio, the same way the agent connects to it.
Run:  python demo_tools.py
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

TOMORROW = (date.today() + timedelta(days=1)).isoformat()


async def run(client: Client, label: str, tool: str, args: dict) -> None:
    print(f"\n=== {label} ===\n{tool}({args})")
    try:
        result = await client.call_tool(tool, args)
        print(json.dumps(result.structured_content.get("result", result.structured_content), indent=2, ensure_ascii=False))
    except ToolError as e:
        print(f"ERROR (handled): {e}")


async def main() -> None:
    # write demo plans to a temp file so the real plan list is not touched
    plans_file = Path(tempfile.gettempdir()) / "outing_demo_plans.json"
    plans_file.unlink(missing_ok=True)
    transport = StdioTransport(
        command=sys.executable,
        args=[str(Path(__file__).parent / "server.py")],
        env={**os.environ, "OUTING_PLANS_FILE": str(plans_file)},
    )
    async with Client(transport) as client:
        print("Tools:", [t.name for t in await client.list_tools()])

        await run(client, "Data tool", "get_weather_forecast", {"city": "Beirut", "date": TOMORROW})
        await run(client, "Action tool", "save_outing_plan",
                  {"title": "Corniche walk", "city": "Beirut", "date": TOMORROW, "activity": "walking"})
        await run(client, "Orchestration tool", "plan_outing",
                  {"city": "Byblos", "date": TOMORROW, "activity": "beach"})

        await run(client, "Failure: unknown city", "get_weather_forecast", {"city": "Qwzxplk", "date": TOMORROW})
        await run(client, "Failure: bad date", "get_weather_forecast", {"city": "Beirut", "date": "next friday"})
        await run(client, "Failure: duplicate plan", "save_outing_plan",
                  {"title": "Corniche walk", "city": "Beirut", "date": TOMORROW, "activity": "walking"})

        print("\n=== Resource plans://saved ===")
        print((await client.read_resource("plans://saved"))[0].text)


if __name__ == "__main__":
    asyncio.run(main())
