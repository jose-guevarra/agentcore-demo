"""Unit tests for the weather_tool Lambda.

weather_tool.py has no AWS dependency at all -- it only makes two outbound
HTTPS calls to Open-Meteo. Rather than mocking urllib (or hitting the real,
live Open-Meteo API and coupling test outcomes to whatever the weather
actually is today), these tests point weather_tool's GEOCODE_URL/FORECAST_URL
constants at a local HTTP server serving canned JSON -- the same "real local
server, not a mock" style test_feed_ingest.py uses for its third-party
article fetches.

Run everything:
    cd src && make weather_tool_test
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "weather_tool"
if str(HANDLER_DIR) not in sys.path:
    sys.path.insert(0, str(HANDLER_DIR))

import weather_tool  # noqa: E402


# --------------------------------------------------------------------------
# Local fixture HTTP server -- stands in for geocoding-api/api.open-meteo.com
# --------------------------------------------------------------------------

class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        body = self.server.pages.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):  # keep pytest output clean
        pass


class FixtureServer:
    def __init__(self):
        self.pages = {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self._httpd.pages = self.pages
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path, body):
        self.pages[path] = body
        return f"http://127.0.0.1:{self._port}{path}"

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def fixture_server():
    server = FixtureServer()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _restore_urls():
    """Every test repoints GEOCODE_URL/FORECAST_URL at the fixture server;
    restore the real Open-Meteo URLs afterwards so tests don't leak state.
    """
    geocode_url, forecast_url = weather_tool.GEOCODE_URL, weather_tool.FORECAST_URL
    yield
    weather_tool.GEOCODE_URL, weather_tool.FORECAST_URL = geocode_url, forecast_url


AUSTIN_GEOCODE = {
    "results": [
        {"name": "Austin", "country": "United States", "latitude": 30.26715, "longitude": -97.74306}
    ]
}
AUSTIN_FORECAST = {
    "current_weather": {
        "temperature": 33.5,
        "windspeed": 12.4,
        "weathercode": 1,
        "time": "2026-08-16T14:00",
    }
}


def test_get_weather_returns_conditions_for_known_city(fixture_server):
    weather_tool.GEOCODE_URL = fixture_server.url("/v1/search", AUSTIN_GEOCODE)
    weather_tool.FORECAST_URL = fixture_server.url("/v1/forecast", AUSTIN_FORECAST)

    result = weather_tool.lambda_handler({"city": "Austin"}, None)

    assert result == {
        "city": "Austin",
        "country": "United States",
        "temperature_c": 33.5,
        "wind_speed_kmh": 12.4,
        "conditions": "Mainly clear",
        "observed_at": "2026-08-16T14:00",
    }


def test_get_weather_unknown_weather_code_falls_back(fixture_server):
    weather_tool.GEOCODE_URL = fixture_server.url("/v1/search", AUSTIN_GEOCODE)
    weather_tool.FORECAST_URL = fixture_server.url(
        "/v1/forecast",
        {"current_weather": {**AUSTIN_FORECAST["current_weather"], "weathercode": 404}},
    )

    result = weather_tool.lambda_handler({"city": "Austin"}, None)

    assert result["conditions"] == "Unknown (404)"


def test_get_weather_missing_city_is_rejected(fixture_server):
    result = weather_tool.lambda_handler({"city": "   "}, None)

    assert result == {
        "error": "missing_city",
        "message": "The 'city' argument is required.",
    }


def test_get_weather_no_geocode_match_returns_not_found(fixture_server):
    weather_tool.GEOCODE_URL = fixture_server.url("/v1/search", {"results": []})

    result = weather_tool.lambda_handler({"city": "Nowhereville"}, None)

    assert result == {
        "error": "not_found",
        "message": "No location found matching 'Nowhereville'",
    }


def test_get_weather_upstream_failure_is_reported_not_raised(fixture_server):
    # No fixture page registered for this path -> the fixture server 404s,
    # which urlopen surfaces as an HTTPError.
    weather_tool.GEOCODE_URL = f"http://127.0.0.1:{fixture_server._port}/v1/search"  # noqa: SLF001

    result = weather_tool.lambda_handler({"city": "Austin"}, None)

    assert result["error"] == "upstream_unavailable"
