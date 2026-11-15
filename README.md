# Better Weather 🌦️

A reliable weather plugin for [Hermes Agent](https://hermes-agent.nousresearch.com) that works the
same on macOS, Windows and Linux.

- **Pure Python, nothing to compile.** Only `httpx` (already part of Hermes) and, on Windows,
  `tzdata`. Works on every Python Hermes supports (3.11 and up), Intel and ARM.
- **Free sources, no keys, no accounts:** the US National Weather Service, Open-Meteo (worldwide)
  and the NOAA Storm Prediction Center.
- **Never fails silently.** Every source has a timeout, retries and a fallback; every answer says
  where it came from and how old it is, and says so plainly when something couldn't be checked.

## Tools

| Tool | What it answers |
|---|---|
| `weather` | Now, the next hours, today/tonight, and any active alerts. No arguments = home. |
| `weather_forecast` | Daily (up to 16 days) or hourly (up to 72 hours). |
| `weather_alerts` | Official warnings, watches and advisories with full text and instructions (US). |
| `weather_severe` | Storm Prediction Center risk at your exact spot (days 1–3), today's tornado/wind/hail odds, watches/warnings, model instability. |
| `weather_places` | Set home, save named places ("work", "mom"), list or remove them. |

Places can be a name ("Des Moines, IA", "Paris, France"), a US ZIP code, an airport code (KDSM),
`lat,lon`, or a saved place name.

**`/weather`** in any chat answers instantly without the AI: `/weather`, `/weather forecast`,
`/weather hourly`, `/weather alerts`, `/weather severe`, each optionally followed by a place.

**Terminal:** `hermes better-weather now|forecast|hourly|alerts|severe [place]`,
`hermes better-weather doctor` (checks every source), `hermes better-weather clear-cache`.

## Install

```bash
hermes plugins install jarretcoon/better-weather
hermes plugins enable better-weather
```

Then set your home in the Desktop app (Settings → Plugins → better-weather), or just tell the
agent where home is.

## Settings

`plugins.entries.better-weather.settings` in `config.yaml` (or the Desktop settings form):

| Key | Default | |
|---|---|---|
| `home` | `""` | Home place, in any of the forms above |
| `units` | `us` | `us` (°F, mph, in) or `metric` (°C, km/h, mm) |
| `clock` | `12h` | `12h` or `24h` |

## How it stays reliable

- Responses are cached in the plugin's own data folder (`plugin-data/better-weather/`): alerts for
  1 minute, observations 5 minutes, forecasts 15–30 minutes, place lookups 30 days.
- If a live request fails, a recent cached copy is used and the answer says how old it is. Alerts
  are only ever reused for 10 minutes; past that the answer says alerts couldn't be checked.
- NWS forecast down → Open-Meteo. Station report missing or stale → model estimate, labelled.
- NWS alert *updates* are kept (an ongoing warning is re-issued as an update; dropping those loses
  warnings that are still in effect).

## Privacy

No accounts or keys. Coordinates are sent rounded (3 decimals, about 100 m, to the NWS; 2 decimals,
about 1 km, to Open-Meteo). Home and saved places stay in your own Hermes config and plugin state.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests            # offline, replays recorded real responses
.venv/bin/python -m pytest tests -m live    # hits the real services
.venv/bin/ruff check . && .venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/python -I tests/record_fixtures.py   # re-record fixtures when a source changes
```

The repo root is the plugin package (Hermes imports it directly), so run pytest on `tests`, not
on the root.
