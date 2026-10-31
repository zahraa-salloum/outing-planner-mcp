"""Outing Planner MCP server (FastMCP).

Three tools, one per category:
    get_weather_forecast  -> Data tool          (reads a forecast, changes nothing)
    save_outing_plan      -> Action tool        (writes a plan to disk)
    plan_outing           -> Orchestration tool (forecast -> advice -> save)

Plus one resource, plans://saved, for reading back what has been saved.

Weather comes from Open-Meteo (https://open-meteo.com), which is free and needs no API key.
"""

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_FORECAST_DAYS = 15  # Open-Meteo gives 16 days including today
PLANS_FILE = Path(os.environ.get("OUTING_PLANS_FILE", Path(__file__).parent / "data" / "plans.json"))
LOG_FILE = Path(os.environ.get("OUTING_LOG_FILE", Path(__file__).parent / "data" / "server.log"))

# WMO weather codes -> readable text (subset Open-Meteo actually returns)
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

mcp = FastMCP(
    name="outing-planner",
    instructions=(
        "Tools for planning outdoor outings. Use get_weather_forecast to look up weather, "
        "save_outing_plan to store a plan the user already decided on, and plan_outing when the "
        "user wants a full plan (forecast + advice + saved) in one go."
    ),
)


def log(msg: str) -> None:
    # stdout is the MCP stdio channel, so server logs must go to stderr
    print(f"[server] {msg}", file=sys.stderr, flush=True)
    # stderr of a stdio subprocess may not reach the console on Windows, so also keep a file to watch live
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} [server] {msg}\n")


# ---------- typed outputs ----------

class Forecast(TypedDict):
    city: str
    country: str
    latitude: float
    longitude: float
    date: str
    conditions: str
    temp_max_c: float
    temp_min_c: float
    precipitation_chance_pct: int
    wind_max_kmh: float


class SavedPlan(TypedDict):
    id: str
    title: str
    city: str
    date: str
    activity: str
    notes: str
    saved_at: str


class OutingPlan(TypedDict):
    forecast: Forecast
    advice: str
    good_for_outdoors: bool
    saved_plan: SavedPlan


# ---------- helpers ----------

def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ToolError(f"Invalid date '{value}'. Use the format YYYY-MM-DD, for example 2026-11-02.")


async def get_json(url: str, params: dict) -> dict:
    """Single place for HTTP calls, so tests can swap it out."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        raise ToolError(f"Weather service is unavailable right now ({type(e).__name__}). Try again in a moment.")


def load_plans() -> list[SavedPlan]:
    if not PLANS_FILE.exists():
        return []
    return json.loads(PLANS_FILE.read_text(encoding="utf-8"))


# ---------- core logic (shared by the tools and the orchestrator) ----------

async def fetch_forecast(city: str, day: str) -> Forecast:
    city = city.strip()
    if not city:
        raise ToolError("City name is empty. Give a city such as 'Beirut' or 'Paris'.")

    target = parse_date(day)
    today = date.today()
    if target < today:
        raise ToolError(f"{target} is in the past. Forecasts are only available from today ({today}) onward.")
    if target > today + timedelta(days=MAX_FORECAST_DAYS):
        raise ToolError(
            f"{target} is too far ahead. Forecasts only go up to {today + timedelta(days=MAX_FORECAST_DAYS)}."
        )

    geo = await get_json(GEOCODE_URL, {"name": city, "count": 1, "language": "en", "format": "json"})
    if not geo.get("results"):
        raise ToolError(f"Could not find a city called '{city}'. Check the spelling or try a nearby larger city.")
    place = geo["results"][0]

    data = await get_json(FORECAST_URL, {
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_speed_10m_max",
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    })
    daily = data.get("daily")
    if not daily or not daily.get("time"):
        raise ToolError(f"The weather service returned no forecast for {city} on {target}.")

    return Forecast(
        city=place["name"],
        country=place.get("country", ""),
        latitude=place["latitude"],
        longitude=place["longitude"],
        date=daily["time"][0],
        conditions=WEATHER_CODES.get(daily["weather_code"][0], f"weather code {daily['weather_code'][0]}"),
        temp_max_c=daily["temperature_2m_max"][0],
        temp_min_c=daily["temperature_2m_min"][0],
        precipitation_chance_pct=daily["precipitation_probability_max"][0] or 0,
        wind_max_kmh=daily["wind_speed_10m_max"][0],
    )


def store_plan(title: str, city: str, day: str, activity: str, notes: str) -> SavedPlan:
    title, city, activity = title.strip(), city.strip(), activity.strip()
    if not title or not city or not activity:
        raise ToolError("title, city and activity are all required and cannot be empty.")
    target = parse_date(day)

    plans = load_plans()
    for p in plans:
        if p["title"].lower() == title.lower() and p["date"] == target.isoformat():
            raise ToolError(f"A plan called '{title}' on {target} already exists (id {p['id']}). Use a different title.")

    plan = SavedPlan(
        id=uuid.uuid4().hex[:8],
        title=title,
        city=city,
        date=target.isoformat(),
        activity=activity,
        notes=notes.strip(),
        saved_at=datetime.now().isoformat(timespec="seconds"),
    )
    plans.append(plan)
    PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLANS_FILE.write_text(json.dumps(plans, indent=2), encoding="utf-8")
    return plan


def assess(forecast: Forecast) -> tuple[bool, str]:
    """Turn a forecast into a go / reconsider verdict with a short reason."""
    warnings = []
    if forecast["precipitation_chance_pct"] >= 60:
        warnings.append(f"{forecast['precipitation_chance_pct']}% chance of rain, bring an umbrella or have an indoor backup")
    if forecast["temp_max_c"] >= 35:
        warnings.append(f"very hot ({forecast['temp_max_c']}°C), go early and bring water")
    if forecast["temp_max_c"] <= 2:
        warnings.append(f"near freezing ({forecast['temp_max_c']}°C), dress warmly")
    if forecast["wind_max_kmh"] >= 40:
        warnings.append(f"strong wind up to {forecast['wind_max_kmh']} km/h")
    if any(w in forecast["conditions"] for w in ("thunderstorm", "heavy", "violent")):
        warnings.append(f"{forecast['conditions']} expected")

    if not warnings:
        return True, (f"Looks good: {forecast['conditions']}, {forecast['temp_min_c']}-{forecast['temp_max_c']}°C, "
                      f"{forecast['precipitation_chance_pct']}% chance of rain.")
    return False, "Reconsider or prepare: " + "; ".join(warnings) + "."


# ---------- MCP tools ----------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_weather_forecast(city: str, date: str) -> Forecast:
    """[Data tool] Look up the daily weather forecast for one city on one date. Changes nothing.

    Args:
        city: City name, e.g. "Beirut" or "Paris". The best matching city is used.
        date: Day to forecast, format YYYY-MM-DD. Must be between today and 15 days from today.

    Returns:
        Forecast with the resolved city, country, conditions, min/max temperature (°C),
        max precipitation chance (%) and max wind speed (km/h).

    Errors: invalid or out of range date, unknown city, weather service unavailable.
    """
    log(f"get_weather_forecast(city={city!r}, date={date!r})")
    return await fetch_forecast(city, date)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
def save_outing_plan(title: str, city: str, date: str, activity: str, notes: str = "") -> SavedPlan:
    """[Action tool] Save an outing plan to the user's plan list. Does not look up weather.

    Args:
        title: Short name for the outing, e.g. "Saturday hike". Must be unique per date.
        city: Where the outing happens.
        date: Day of the outing, format YYYY-MM-DD.
        activity: What the user will do, e.g. "hiking", "picnic", "beach".
        notes: Optional free text to keep with the plan.

    Returns:
        The saved plan including its generated id and save timestamp.

    Errors: empty required fields, invalid date, a plan with the same title already saved for that date.
    """
    log(f"save_outing_plan(title={title!r}, city={city!r}, date={date!r}, activity={activity!r})")
    return store_plan(title, city, date, activity, notes)


@mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
async def plan_outing(city: str, date: str, activity: str, title: str = "") -> OutingPlan:
    """[Orchestration tool] Plan an outing end to end: fetch the forecast, judge whether the weather
    suits an outdoor activity, then save the plan with that advice in its notes.

    Use this when the user wants a complete plan. For just the weather use get_weather_forecast,
    and for saving something already decided use save_outing_plan.

    Args:
        city: City name, e.g. "Beirut".
        date: Day of the outing, format YYYY-MM-DD, between today and 15 days from today.
        activity: What the user wants to do, e.g. "hiking".
        title: Optional plan name. Defaults to "<activity> in <city>".

    Returns:
        The forecast, a go/reconsider verdict (good_for_outdoors), the advice text, and the saved plan.

    Errors: anything the forecast or save step raises. Nothing is saved if the forecast step fails.
    """
    log(f"plan_outing(city={city!r}, date={date!r}, activity={activity!r})")
    log("  step 1/3: fetching forecast")
    forecast = await fetch_forecast(city, date)
    log("  step 2/3: assessing weather")
    ok, advice = assess(forecast)
    log("  step 3/3: saving plan")
    saved = store_plan(
        title=title or f"{activity.strip().capitalize()} in {forecast['city']}",
        city=forecast["city"],
        day=forecast["date"],
        activity=activity,
        notes=advice,
    )
    return OutingPlan(forecast=forecast, advice=advice, good_for_outdoors=ok, saved_plan=saved)


# ---------- MCP resource ----------

@mcp.resource("plans://saved", mime_type="application/json")
def saved_plans() -> str:
    """All outing plans saved so far, as a JSON list."""
    return json.dumps(load_plans(), indent=2)


if __name__ == "__main__":
    log("server started, waiting for MCP requests over stdio")
    mcp.run(show_banner=False)
