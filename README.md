# Outing Planner MCP Toolkit

An MCP server built with **FastMCP**, driven by an agent built with the **OpenAI Agents SDK**.
The agent helps you plan outdoor outings using live weather from [Open-Meteo](https://open-meteo.com) (free, no API key).

EECE 503P/798S Agentic Systems, C5 Agent Tools, Zahraa Salloum (202674524)

## The three tools

| Category | Tool | What it does | Failure cases handled |
|---|---|---|---|
| **Data** | `get_weather_forecast(city, date)` | Looks up the daily forecast for one city on one date. Read only. | bad date format, date in the past, date more than 15 days out, unknown city, weather service unreachable |
| **Action** | `save_outing_plan(title, city, date, activity, notes="")` | Saves a plan to `data/plans.json`. Does not look up weather. | empty fields, bad date, duplicate title on the same date |
| **Orchestration** | `plan_outing(city, date, activity, title="")` | Fetches the forecast, judges whether it suits an outdoor activity, then saves the plan with that advice as its notes. | propagates errors from either step, and saves nothing if the forecast fails |

There is also one **resource**, `plans://saved`, which returns every saved plan as JSON (a pure data lookup).

All tools return typed outputs (`Forecast`, `SavedPlan`, `OutingPlan` TypedDicts), so FastMCP publishes an output schema for each. Errors are raised as `ToolError` with a message the model can read and explain to the user.

## Architecture

```
 you ──> agent.py (MCP Host, OpenAI Agents SDK)
            │  MCPServerStdio = MCP Client
            │  JSON-RPC over stdio
            ▼
         server.py (MCP Server, FastMCP) ──HTTP──> Open-Meteo geocoding + forecast APIs
                                         └──────> data/plans.json
```

`agent.py` prints every tool call the model makes (yellow), the server's reply (grey) and the final answer (green). `server.py` logs each call with a `[server]` prefix to stderr and to `data/server.log`. On Windows the stderr output may not reach the console, so watch the server side from a second terminal: `Get-Content data\server.log -Wait -Tail 5`.

## Setup

Python **3.12+** is recommended. `openai-agents` fails to import on Python 3.11.0 because of a typing bug in that early release.

```bash
cd outing-planner-mcp
python -m venv .venv            # or: uv venv --python 3.12 .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### Model configuration

Copy `.env.example` to `.env` and fill in one of the options:

- **OpenAI**: set `OPENAI_API_KEY`, and optionally `AGENT_MODEL` (for example `gpt-4.1-mini`). If `AGENT_MODEL` is not set, the SDK's default model is used.
- **Any OpenAI-compatible provider** (Groq, Gemini, OpenRouter, a local Ollama): set `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `AGENT_MODEL`. The model must support tool calling.

## Running

```bash
python agent.py                                       # interactive chat (type "exit" to quit)
python agent.py "What's the weather in Paris tomorrow?"  # one-shot question
python demo_tools.py                                  # calls each tool directly over stdio against the live API, no LLM
pytest -v                                             # offline unit tests, including every failure case
```

Example prompts for the demo:

1. *"What's the weather like in Beirut tomorrow?"* → Data tool
2. *"Save a plan called Sunday brunch in Byblos this Sunday, activity brunch."* → Action tool
3. *"Plan a hike in Chamonix on Saturday."* → Orchestration tool
4. *"What's the weather in Qwzxplk tomorrow?"* or *"...in Paris next year?"* → clear error, explained by the agent

## Tests

`tests/test_server.py` connects a real FastMCP `Client` to the server in memory and calls the tools exactly as an MCP client would. The Open-Meteo calls are replaced with canned responses, so the tests are fast, give the same result every run and need no network. Coverage:

- discovery: the server exposes exactly the three tools
- Data tool: typed output, writes nothing, unknown city, bad date format (fails before any HTTP call), past date, too far ahead, service unreachable
- Action tool: writes to disk, rejects duplicates, rejects empty fields
- Orchestration tool: good weather, bad weather warnings, saves nothing when the forecast fails
- Resource: `plans://saved` lists saved plans

## Files

```
server.py          FastMCP server: 3 tools + 1 resource
agent.py           OpenAI Agents SDK host/client (interactive)
demo_tools.py      direct tool calls over stdio, including failure cases
tests/             pytest suite
requirements.txt
.env.example
```
