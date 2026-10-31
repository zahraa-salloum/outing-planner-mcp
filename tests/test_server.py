"""Tests for the three MCP tools and the resource.

Every test talks to the server through a real FastMCP Client (in-memory transport), so the tools
are exercised exactly as an MCP client would call them. The Open-Meteo HTTP calls are replaced
with canned responses so the tests are fast and do not depend on the network.
Run:  pytest -v
"""

import json
from datetime import date, timedelta

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server

TOMORROW = (date.today() + timedelta(days=1)).isoformat()

GEO_BEIRUT = {"results": [{"name": "Beirut", "country": "Lebanon", "latitude": 33.89, "longitude": 35.5}]}


def forecast_response(day, code=0, tmax=24.0, tmin=17.0, rain=10, wind=12.0):
    return {"daily": {
        "time": [day], "weather_code": [code],
        "temperature_2m_max": [tmax], "temperature_2m_min": [tmin],
        "precipitation_probability_max": [rain], "wind_speed_10m_max": [wind],
    }}


@pytest.fixture(autouse=True)
def isolated_plans(tmp_path, monkeypatch):
    """Each test writes to its own empty plans and log files."""
    monkeypatch.setattr(server, "PLANS_FILE", tmp_path / "plans.json")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "server.log")


@pytest.fixture
def fake_weather(monkeypatch):
    """Replace HTTP with canned data. Tests can change `responses` to simulate other weather."""
    responses = {"geo": GEO_BEIRUT, "forecast": forecast_response(TOMORROW)}
    calls = []

    async def fake_get_json(url, params):
        calls.append(url)
        return responses["geo"] if url == server.GEOCODE_URL else responses["forecast"]

    monkeypatch.setattr(server, "get_json", fake_get_json)
    return responses, calls


async def call(tool, **args):
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, args)
        return result.structured_content.get("result", result.structured_content)


# ---------- discovery ----------

async def test_server_exposes_exactly_three_tools():
    async with Client(server.mcp) as client:
        names = sorted(t.name for t in await client.list_tools())
    assert names == ["get_weather_forecast", "plan_outing", "save_outing_plan"]


# ---------- Data tool ----------

async def test_forecast_returns_typed_fields(fake_weather):
    f = await call("get_weather_forecast", city="beirut", date=TOMORROW)
    assert f["city"] == "Beirut" and f["country"] == "Lebanon"
    assert f["conditions"] == "clear sky"
    assert f["temp_max_c"] == 24.0 and f["precipitation_chance_pct"] == 10


async def test_forecast_does_not_write_anything(fake_weather):
    await call("get_weather_forecast", city="Beirut", date=TOMORROW)
    assert not server.PLANS_FILE.exists()


async def test_forecast_unknown_city_fails_clearly(fake_weather):
    fake_weather[0]["geo"] = {}  # geocoder found nothing
    with pytest.raises(ToolError, match="Could not find a city called 'Atlantis'"):
        await call("get_weather_forecast", city="Atlantis", date=TOMORROW)


async def test_forecast_bad_date_format_fails_before_any_http(fake_weather):
    with pytest.raises(ToolError, match="Use the format YYYY-MM-DD"):
        await call("get_weather_forecast", city="Beirut", date="next friday")
    assert fake_weather[1] == []  # no network calls were made


async def test_forecast_past_date_fails(fake_weather):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(ToolError, match="in the past"):
        await call("get_weather_forecast", city="Beirut", date=yesterday)


async def test_forecast_too_far_ahead_fails(fake_weather):
    far = (date.today() + timedelta(days=30)).isoformat()
    with pytest.raises(ToolError, match="too far ahead"):
        await call("get_weather_forecast", city="Beirut", date=far)


async def test_forecast_service_down_fails_clearly(monkeypatch):
    def offline(request):
        raise httpx.ConnectError("no network")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(offline), **kw))
    with pytest.raises(ToolError, match="Weather service is unavailable"):
        await call("get_weather_forecast", city="Beirut", date=TOMORROW)


# ---------- Action tool ----------

async def test_save_plan_writes_to_disk():
    plan = await call("save_outing_plan", title="Beach day", city="Byblos", date=TOMORROW,
                      activity="swimming", notes="bring sunscreen")
    assert plan["title"] == "Beach day" and len(plan["id"]) == 8
    stored = json.loads(server.PLANS_FILE.read_text())
    assert stored == [plan]


async def test_save_duplicate_plan_fails():
    await call("save_outing_plan", title="Beach day", city="Byblos", date=TOMORROW, activity="swimming")
    with pytest.raises(ToolError, match="already exists"):
        await call("save_outing_plan", title="beach DAY", city="Byblos", date=TOMORROW, activity="swimming")


async def test_save_empty_title_fails():
    with pytest.raises(ToolError, match="cannot be empty"):
        await call("save_outing_plan", title="  ", city="Byblos", date=TOMORROW, activity="swimming")


# ---------- Orchestration tool ----------

async def test_plan_outing_good_weather_saves_plan(fake_weather):
    out = await call("plan_outing", city="Beirut", date=TOMORROW, activity="hiking")
    assert out["good_for_outdoors"] is True
    assert out["advice"].startswith("Looks good")
    assert out["saved_plan"]["title"] == "Hiking in Beirut"
    assert out["saved_plan"]["notes"] == out["advice"]  # forecast result fed into the save step
    assert len(json.loads(server.PLANS_FILE.read_text())) == 1


async def test_plan_outing_bad_weather_warns(fake_weather):
    fake_weather[0]["forecast"] = forecast_response(TOMORROW, code=95, rain=85, wind=55.0)
    out = await call("plan_outing", city="Beirut", date=TOMORROW, activity="picnic")
    assert out["good_for_outdoors"] is False
    assert "85% chance of rain" in out["advice"] and "thunderstorm" in out["advice"]


async def test_plan_outing_saves_nothing_when_forecast_fails(fake_weather):
    fake_weather[0]["geo"] = {}
    with pytest.raises(ToolError, match="Could not find a city"):
        await call("plan_outing", city="Atlantis", date=TOMORROW, activity="hiking")
    assert not server.PLANS_FILE.exists()


# ---------- Resource ----------

async def test_saved_plans_resource_lists_plans():
    await call("save_outing_plan", title="Picnic", city="Beirut", date=TOMORROW, activity="picnic")
    async with Client(server.mcp) as client:
        contents = await client.read_resource("plans://saved")
    plans = json.loads(contents[0].text)
    assert [p["title"] for p in plans] == ["Picnic"]
