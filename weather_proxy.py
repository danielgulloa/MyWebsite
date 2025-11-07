#!/usr/bin/env python3
"""Simple weather proxy for legacy browsers.

Run this on a modern computer that can reach https://api.weather.gov and set
`weather.proxyUrl` in config.json to point legacy devices at this endpoint,
for example: "http://192.168.1.10:8050/weather".

The proxy combines the latest observation with the first three days of the
forecast into a single response the dashboard can consume without contacting
the National Weather Service directly.
"""

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CACHE_LOCK = threading.Lock()
CACHE_TTL = 300  # seconds
_cache_payload = None
_cache_timestamp = 0.0


class ProxyError(Exception):
    """Raised when the proxy cannot fetch or parse remote data."""


def _format_user_agent(raw):
    raw = (raw or "").strip()
    if not raw:
        return "LegacyDashboardProxy/1.0 (contact unavailable)"
    if "/" not in raw and "(" not in raw:
        return f"LegacyDashboardProxy/1.0 ({raw})"
    return raw


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        raise ProxyError("config.json not found alongside weather_proxy.py")

    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    weather = data.get("weather", {})
    latitude = weather.get("latitude")
    longitude = weather.get("longitude")
    contact = (weather.get("userAgent") or "").strip()

    if latitude is None or longitude is None:
        raise ProxyError("config.json must supply weather.latitude and weather.longitude")

    return float(latitude), float(longitude), contact


def _make_request(url, headers):
    request = urllib.request.Request(url)
    for key, value in headers.items():
        if value is not None:
            request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _fetch_from_nws():
    latitude, longitude, contact = _load_config()

    headers = {
        "Accept": "application/geo+json",
        "User-Agent": _format_user_agent(contact),
        "From": contact or None,
    }

    point_url = f"https://api.weather.gov/points/{latitude},{longitude}"

    point_data = _make_request(point_url, headers)
    properties = point_data.get("properties") or {}
    forecast_url = properties.get("forecast")
    stations_url = properties.get("observationStations")

    if not forecast_url:
        raise ProxyError("Forecast URL unavailable for configured coordinates")

    forecast_data = _make_request(forecast_url, headers)
    properties = forecast_data.get("properties") or {}
    periods = properties.get("periods")
    if isinstance(periods, list) and periods:
        # Keep only the first six forecast periods (roughly three days)
        properties = dict(properties)
        properties["periods"] = periods[:6]
        forecast_data["properties"] = properties

    observation = None
    if stations_url:
        stations_data = _make_request(stations_url, headers)
        features = stations_data.get("features") or []
        if features:
            latest_url = features[0].get("id")
            if latest_url:
                latest_url = urllib.parse.urljoin(latest_url + "/", "observations/latest")
                try:
                    observation = _make_request(latest_url, headers)
                except (urllib.error.URLError, ProxyError, TimeoutError, ValueError):
                    observation = None

    return {
        "forecast": forecast_data.get("properties", {}),
        "observation": observation.get("properties") if isinstance(observation, dict) else None,
    }


def _get_cached_weather():
    global _cache_payload, _cache_timestamp
    with CACHE_LOCK:
        now = time.time()
        if _cache_payload is not None and now - _cache_timestamp < CACHE_TTL:
            return _cache_payload

        payload = _fetch_from_nws()
        _cache_payload = payload
        _cache_timestamp = now
        return payload


class WeatherProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quiet the default logging to keep terminal output tidy.
        pass

    def do_GET(self):
        if self.path.rstrip("/") not in {"", "/weather"}:
            self.send_error(404, "Not Found")
            return

        try:
            payload = _get_cached_weather()
        except ProxyError as exc:
            self.send_error(502, str(exc))
            return
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self.send_error(502, f"Upstream request failed: {exc}")
            return

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Serve National Weather Service data to legacy browsers.")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8050, help="Port to listen on (default: 8050)")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), WeatherProxyHandler)
    print(f"Weather proxy listening on http://{args.host}:{args.port}/weather")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping proxy...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
