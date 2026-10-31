"""Outing Planner agent: the MCP Host/Client built with the OpenAI Agents SDK.

The agent launches server.py as a subprocess over stdio, discovers its three tools through MCP,
and lets the model decide which tool to call for each user request.

Run:  python agent.py                  (interactive chat)
      python agent.py "your question"   (single question)
"""

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from agents import Agent, Runner, OpenAIChatCompletionsModel, set_tracing_disabled
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.mcp import MCPServerStdio

load_dotenv()

SERVER_PATH = Path(__file__).parent / "server.py"

# terminal colours so the round trip is easy to follow on screen
CYAN, YELLOW, GREEN, DIM, RESET = "\033[36m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def build_model():
    """Use OpenAI by default, or any OpenAI-compatible endpoint if OPENAI_BASE_URL is set."""
    model_name = os.environ.get("AGENT_MODEL")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not base_url:
        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit("OPENAI_API_KEY is not set. Copy .env.example to .env and add a key (see README).")
        return model_name  # None -> Agents SDK default OpenAI model
    if not model_name:
        sys.exit("AGENT_MODEL must be set when OPENAI_BASE_URL is used (see README).")
    set_tracing_disabled(True)  # tracing uploads to OpenAI, not available on other providers
    client = AsyncOpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "none"))
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def instructions() -> str:
    return (
        f"You are a friendly outing planner. Today is {date.today().isoformat()} "
        f"({date.today().strftime('%A')}). Always convert relative days like 'tomorrow' or "
        "'Saturday' into YYYY-MM-DD before calling a tool.\n"
        "Pick exactly the tool that fits:\n"
        "- only asking about weather -> get_weather_forecast\n"
        "- asking to save an outing they already decided on -> save_outing_plan\n"
        "- asking you to plan an outing (check weather and save it) -> plan_outing\n"
        "If a tool returns an error, explain it to the user in plain words and suggest a fix. "
        "Never invent weather data."
    )


def show_round_trip(result) -> None:
    """Print each tool call the model made and what the MCP server sent back."""
    for item in result.new_items:
        if isinstance(item, ToolCallItem):
            raw = item.raw_item
            name = getattr(raw, "name", None) or raw.get("name")
            args = getattr(raw, "arguments", None) or raw.get("arguments")
            print(f"{YELLOW}  -> agent calls MCP tool {name}({args}){RESET}")
        elif isinstance(item, ToolCallOutputItem):
            out = item.output if isinstance(item.output, str) else json.dumps(item.output)
            print(f"{DIM}  <- server returned: {out[:400]}{'...' if len(out) > 400 else ''}{RESET}")


async def chat(agent: Agent, questions: list[str] | None) -> None:
    history = []
    while True:
        if questions is not None:
            if not questions:
                return
            user = questions.pop(0)
            print(f"{CYAN}you> {user}{RESET}")
        else:
            try:
                user = input(f"{CYAN}you> {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if user.lower() in {"exit", "quit"}:
                return
            if not user:
                continue

        result = await Runner.run(agent, history + [{"role": "user", "content": user}])
        show_round_trip(result)
        print(f"{GREEN}agent> {result.final_output}{RESET}\n")
        history = result.to_input_list()


async def main() -> None:
    async with MCPServerStdio(
        name="outing-planner",
        params={"command": sys.executable, "args": [str(SERVER_PATH)]},
        cache_tools_list=True,
        client_session_timeout_seconds=30,
    ) as server:
        tools = await server.list_tools()
        print(f"{DIM}Connected to MCP server 'outing-planner', tools: {[t.name for t in tools]}{RESET}\n")

        agent = Agent(
            name="Outing Planner",
            instructions=instructions(),
            model=build_model(),
            mcp_servers=[server],
        )
        await chat(agent, sys.argv[1:] or None)


if __name__ == "__main__":
    asyncio.run(main())
