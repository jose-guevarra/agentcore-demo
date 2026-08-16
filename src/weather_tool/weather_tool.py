"""Weather-lookup Lambda backing the `get_weather` MCP tool.

Invoked directly by AgentCore Gateway's Lambda target
(infra/acdemo/gateway.tf's aws_bedrockagentcore_gateway_target.weather_target) --
the event *is* the tool's input arguments as defined by that target's inline
tool_schema, not an API Gateway proxy event. The Gateway only exposes this one
tool off this Lambda, so there's no tool-name dispatch to do here (contrast
multi-tool Lambda targets, which read the invoked tool's name from
context.client_context.custom["bedrockAgentCoreToolName"]).

Unlike feed_ingest.py's lambda_handler (which wraps every response in an
API-Gateway-style {"statusCode", "body"} envelope for its own Lambda-invokes-
Lambda plumbing), this handler returns its result dict directly -- that's what
the Gateway hands back to the MCP client as the tool's output.

Calls Open-Meteo's free, keyless public APIs (https://open-meteo.com) -- no
AWS SDK calls and no secrets to manage, so this Lambda's IAM role needs
nothing beyond basic CloudWatch Logs write access.
"""

import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 8  # fail fast; Gateway's own target-invoke timeout is short
USER_AGENT = "Mozilla/5.0 (compatible; weather-tool/1.0; +https://github.com/agentcore-demo)"

# https://open-meteo.com/en/docs -- WMO weather interpretation codes, as returned by
# the current_weather=true field.
WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


# --------------------------------------------------------------------------
# Open-Meteo HTTP calls
# --------------------------------------------------------------------------

def _get_json(url: str, params: dict) -> dict:
    """GET url?params and parse the JSON body. Raises HTTPError/URLError on
    transport failure -- callers decide how to turn that into a tool error.
    """
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https hosts above
        return json.loads(response.read())


def _geocode(city: str) -> dict:
    """Resolve a free-text city name to Open-Meteo's best-match place record
    (name, country, latitude, longitude). Raises ValueError if nothing matches.
    """
    data = _get_json(GEOCODE_URL, {"name": city, "count": 1})
    results = data.get("results") or []
    if not results:
        raise ValueError(f"No location found matching {city!r}")
    return results[0]


def _current_weather(latitude: float, longitude: float) -> dict:
    data = _get_json(
        FORECAST_URL,
        {"latitude": latitude, "longitude": longitude, "current_weather": "true"},
    )
    return data.get("current_weather") or {}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def lambda_handler(event, context):
    """event is the get_weather tool's input directly: {"city": "<name>"}."""
    city = ((event or {}).get("city") or "").strip()
    if not city:
        return {"error": "missing_city", "message": "The 'city' argument is required."}

    try:
        place = _geocode(city)
        current = _current_weather(place["latitude"], place["longitude"])
    except ValueError as e:
        return {"error": "not_found", "message": str(e)}
    except (HTTPError, URLError) as e:  # noqa: BLE001 - surfaced to the caller as a tool error, not a Lambda failure
        print(f"ERROR: weather_tool upstream call failed for city={city!r}: {e}")
        return {"error": "upstream_unavailable", "message": f"Weather service error: {e}"}

    code = current.get("weathercode")
    return {
        "city": place.get("name", city),
        "country": place.get("country"),
        "temperature_c": current.get("temperature"),
        "wind_speed_kmh": current.get("windspeed"),
        "conditions": WEATHER_CODES.get(code, f"Unknown ({code})"),
        "observed_at": current.get("time"),
    }
