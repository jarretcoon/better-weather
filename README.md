# Better Weather 🌦️

A reliable weather plugin for [Hermes Agent](https://hermes-agent.nousresearch.com) that works the
same on macOS, Windows and Linux.

- **Pure Python, nothing to compile.** Only `httpx` (already part of Hermes) and, on Windows,
  `tzdata`. Works on every Python Hermes supports (3.11 and up), Intel and ARM.
- **Free sources, no keys, no accounts:** the US National Weather Service, Open-Meteo (worldwide),
  the NOAA Storm Prediction Center, NOAA GOES satellites, NOAA's ACIS climate records and the Iowa
  Environmental Mesonet.
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
| `weather_radar` | Radar picture (US): local, regional or national, still or animated, warning boxes drawn in. |
| `weather_satellite` | GOES satellite picture: true color, infrared or water vapor, still or animated. |
| `weather_air` | Air quality (AQI, smoke, ozone), UV, and pollen in Europe; now and the worst hour ahead. |
| `weather_history` | Past days (up to 31 at a time). US: official station records with normals and record highs/lows. |

Pictures come back as `MEDIA:/path` lines, which Hermes sends as real images in Telegram and shows
inline in the Desktop app.

Places can be a name ("Des Moines, IA", "Paris, France"), a US ZIP code, an airport code (KDSM),
`lat,lon`, or a saved place name.

**`/weather`** in any chat answers instantly without the AI: `/weather`, `/weather forecast`,
`/weather hourly`, `/weather alerts`, `/weather severe`, `/weather radar [loop]`,
`/weather satellite [loop]`, `/weather air`, `/weather history [date]`, each optionally followed by a place.

## Warning pushes (zero tokens)

A Hermes cron job in no-agent mode runs a tiny script every 2 minutes. It prints, and so messages
you, only when something changes: a new warning or watch for your place, an extension, or the end
of one. Quiet checks send nothing and use no model at all.

```bash
hermes better-weather push setup                 # home, warnings + watches
hermes better-weather push setup --freeze --rain # plus first-freeze and rain/snow-soon heads-ups
hermes cron create "every 2m" --no-agent --script better-weather-alerts.py --deliver telegram --name better-weather-alerts
hermes better-weather push check                 # what a run would send right now (sends nothing)
hermes better-weather push off                   # pause (push on resumes)
```

It keys each alert on the NWS event id, so routine updates stay quiet; cancellations and upgrades
end the old alert right away, and an alert must be missing from two checks in a row before it is
called ended. The freeze heads-up comes at 3 PM, only for the first freezing night after a milder stretch (not
every night of winter). The rain/snow heads-up comes only when it's dry now and rain or snow is
likely within the hour, between 7 AM and 10 PM, at most every 3 hours. If the alert service is
unreachable for about 20 minutes it tells you once, and again
when it's back. Scheduled AI briefs are blueprints: `/blueprint better-weather:morning-brief`,
`better-weather:week-ahead`, `better-weather:weekend-plan`.

**Terminal:** `hermes better-weather now|forecast|hourly|alerts|severe|radar|satellite|air|history [place]`,
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
  1 minute, observations 5 minutes, forecasts 15–30 minutes, place lookups 30 days. Pictures are
  kept 24 hours.
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
