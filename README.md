# Better Weather 🌦️

**Your Hermes agent's weather desk.**

Ask your agent about the weather the way you'd ask a friend who happens to be a meteorologist, and
get a straight answer from the official sources: what it's doing now, what's coming, whether to
worry, and how sure anyone really is. Answers arrive as clean chat cards, radar and satellite come
through as real pictures, and warnings can reach your phone on their own without spending a single
AI token.

> **Des Moines, IA** · 4:58 AM\
> 🌙 **28°F** clear · feels 25°\
> 💨 E 3 mph · 💧 86%\
> ⏱️ 5 AM 🌙 30° · 7 AM 🌤️ 29° · 9 AM ☁️ 34° · 11 AM ☀️ 42°
>
> **Overnight** ☁️ 26° low · Partly Cloudy\
> **Sunday** 🌤️ 51° high · Mostly Sunny
>
> ✅ No alerts\
> 🌔 Waxing gibbous moon · 63% lit · rises 1:34 PM\
> 🌅 Sunrise 7:03 AM · 🌕 Full moon Tue, Nov 24

### Things you can ask
- "Do I need a jacket?" · "When does the rain start tomorrow?"
- "Any storms coming this week? What's the tornado risk here?" · "Show me the radar."
- "How sure are they about snow on Thursday?" · "Why is the forecast so uncertain?"
- "Best time to mow this weekend?" · "Good night for stargazing?"
- "Will my flight out of O'Hare be delayed, and what's it like when I land in Denver?"
- "Was yesterday a record?" · "Which forecast has been most accurate here lately?"

### What makes it different
- **Official sources first, and it shows its work.** A real station report beats a model guess;
  forecasts and warnings come from the National Weather Service, the Storm Prediction Center,
  Environment Canada and MeteoAlarm; every answer says where it came from and how old it is.
- **Never a false all-clear.** Warnings are matched to your exact spot. If they couldn't be checked
  (a service is down, or there's no official feed there), it says so instead of "no alerts".
- **Made for chat.** Cards read well in Telegram, the Desktop app and the terminal; pictures arrive
  as images; `/weather` answers instantly without the AI at all. By day the card gives sunrise and
  sunset; by night it leads with the moon (its phase, how much is lit, when it sets or rises) and the
  next sunrise, worked out on your machine, no extra service.
- **As deep as you want to go.** How sure the forecast is (80+ model runs), the forecasters' own
  reasoning, storm science (CAPE, shear, ECAPE), and a scorecard of which forecast has been right
  at your home.
- **Light and everywhere.** Pure Python with nothing to compile, the same on macOS, Windows and
  Linux. Free public sources: no keys, no accounts, no telemetry.

## Tools

| Tool | What it answers |
|---|---|
| `weather` | Now, the next hours, today/tonight, and any active alerts. No arguments = home. |
| `weather_forecast` | Daily (up to 16 days) or hourly (up to 72 hours). |
| `weather_alerts` | Official warnings with full text: US (NWS) and Canada matched to your exact spot; 38 European countries via MeteoAlarm. |
| `weather_severe` | Storm Prediction Center risk at your exact spot (days 1–3), today's tornado/wind/hail odds, watches/warnings, model instability. |
| `weather_places` | Set home, save named places ("work", "mom"), list or remove them. |
| `weather_radar` | Radar picture (US): local, regional or national, still or animated, warning boxes drawn in. |
| `weather_satellite` | GOES satellite picture: true color, infrared or water vapor, still or animated. |
| `weather_air` | Air quality (AQI, smoke, ozone), UV, and pollen in Europe; now and the worst hour ahead. |
| `weather_history` | Past days (up to 31 at a time). US: official station records with normals and record highs/lows. |
| `weather_confidence` | How sure the forecast is: odds of rain/snow and big amounts, likely high/low ranges, from 80+ model runs. |
| `weather_discussion` | The NWS forecasters' own write-up (key messages, reasoning, uncertainty) for a US place. |
| `weather_best_time` | Best hours for walk, run, bike, golf, mow, grill, outdoor, car wash or stargazing. |
| `weather_scorecard` | Which forecast (NWS or GFS/ECMWF/ICON/GEM/blend) has been most accurate at home; learns daily. |
| `weather_travel` | Airport conditions, forecast and FAA delays/closures, or weather at a destination when you arrive. |
| `weather_water` | River levels vs flood stage (US), tide times (US coasts) and waves (coasts and big lakes). |
| `weather_science` | Storm science from a model sounding (GFS or HRRR): CAPE/CIN, ECAPE, shear, helicity, STP/SCP, with a plain reading. |

`weather`, `weather_forecast` and `weather_alerts` also return a `card`: a compact, emoji-formatted
block (no tables) that reads well in Telegram, the Desktop app and the terminal, and that the agent
sends as-is. `/weather` replies use the same cards.

Pictures come back as `MEDIA:/path` lines, which Hermes sends as real images in Telegram and shows
inline in the Desktop app.

Places can be a name ("Des Moines, IA", "Paris, France"), a US ZIP code, an airport code (KDSM),
`lat,lon`, or a saved place name.

**`/weather`** in any chat answers instantly without the AI: `/weather`, `/weather forecast`,
`/weather hourly`, `/weather alerts`, `/weather severe`, `/weather radar [loop]`,
`/weather satellite [loop]`, `/weather air`, `/weather history [date]`, each optionally followed by a place.

## Hermes Desktop chip

The package also ships a small Hermes Desktop status-bar item (`desktop/plugin.js`) showing home's
temperature and any active alert (🚨 for life-threatening ones). Click it for the full brief. It's
off by default: turn it on in Desktop under **Capabilities → Plugins → Better Weather**. Its data
comes from the plugin's own backend route (`dashboard/plugin_api.py`, `GET /api/plugins/better-weather/now`),
refreshed every 10 minutes.

## Warning pushes (zero tokens)

A Hermes cron job in no-agent mode runs a tiny script every 2 minutes. It prints, and so messages
you, only when something changes: a new warning or watch for your place, an extension, or the end
of one. Quiet checks send nothing and use no model at all. The pushes are the NWS's alerts, so
they're US-only: `push setup` refuses a place outside the NWS's area.

```bash
hermes better-weather push setup                 # home, warnings + watches
hermes better-weather push setup --freeze --rain # plus first-freeze and rain/snow-soon heads-ups
hermes cron create "every 2m" --no-agent --script better-weather-alerts.py --deliver telegram --name better-weather-alerts
hermes better-weather push check                 # what a run would send right now (sends nothing)
hermes better-weather push off                   # pause (push on resumes)
```

It keys each alert on the NWS event id, so routine updates stay quiet. The NWS's feed leaves out
cancellation and upgrade notices, so an alert must be missing from two checks in a row (about 4
minutes) before it is called ended; one replaced by a stronger alert from the same office (a watch
by its warning, a gale warning by a storm warning) is called upgraded, not ended. An expiry notice
ends an alert right away. An alert that starts later says so ("from tomorrow 3 AM to 9 PM").
The freeze heads-up comes at 3 PM, only for the first freezing night after a milder stretch (not
every night of winter). The rain/snow heads-up comes only when it's dry now and rain or snow is
likely within the hour, between 7 AM and 10 PM, at most every 3 hours. If the alert service is
unreachable for about 20 minutes it tells you once, and again
when it's back. Runs never overlap, and a run that takes longer than 100 seconds gives up and counts
as a failed check. Scheduled AI briefs are blueprints: `/blueprint better-weather:morning-brief`,
`better-weather:week-ahead`, `better-weather:weekend-plan`.

**Terminal:** `hermes better-weather now|forecast|hourly|alerts|severe|radar|satellite|air|history [place]`,
`hermes better-weather doctor` (checks every source), `hermes better-weather scorecard [update]`,
`hermes better-weather clear-cache`.

## Install

Needs Hermes Agent 0.23.1 or newer.

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

## What it sends and stores

- **Network:** read-only HTTPS requests to the public services listed below, and nothing else. Each
  request identifies itself as the Better Weather plugin. No accounts, keys or telemetry.
  Coordinates are rounded (3 decimals, about 100 m, for the NWS; 2 decimals, about 1 km, for
  Open-Meteo).
- **Files:** only Hermes' data folder for this plugin (`plugin-data/better-weather/`: response
  cache, scorecard, saved places, pictures kept 24 hours, push state). Home and units live in your
  Hermes config. If you set up warning pushes, `push setup` also writes one script,
  `$HERMES_HOME/scripts/better-weather-alerts.py`, because Hermes runs no-agent cron scripts only
  from there.
- **Background work:** the forecast scorecard checks the home forecast a few times a day (a Hermes
  idle task; on Hermes versions without idle tasks it runs after a `weather` question instead, at
  most every 6 hours). The warning push runs only if you create its cron job yourself.
- **Nothing else:** no shell commands, no credentials, no reading outside its own data.

## Data sources and terms

| Source | Used for | Terms |
|---|---|---|
| [National Weather Service](https://www.weather.gov) (api.weather.gov), NWS radar, NWPS rivers | US conditions, forecasts, alerts, discussions, radar, river gauges | US government work, public domain |
| [NOAA Storm Prediction Center](https://www.spc.noaa.gov) | Severe-storm outlooks | Public domain |
| [NOAA GOES / STAR](https://www.star.nesdis.noaa.gov/GOES/) | Satellite pictures | Public domain |
| [NOAA ACIS](https://www.rcc-acis.org) | US station history, normals and records | Public domain |
| [NOAA CO-OPS](https://tidesandcurrents.noaa.gov) | Tide times | Public domain |
| [aviationweather.gov](https://aviationweather.gov), [FAA NAS status](https://nasstatus.faa.gov) | Airport reports, forecasts, delays | Public domain |
| [Open-Meteo](https://open-meteo.com) | Worldwide forecasts, place search, ensembles, air quality, history (ERA5), marine | Free for **non-commercial** use; data under CC BY 4.0, "Weather data by Open-Meteo.com" |
| [Environment and Climate Change Canada](https://weather.gc.ca) (GeoMet) | Canadian alerts | Open Government Licence – Canada |
| [MeteoAlarm](https://www.meteoalarm.org) (EUMETNET) | Alerts in 38 European countries | MeteoAlarm terms of use |
| [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu) | Backup radar pictures; backup copy of NWS warnings when the NWS alerts service is down | Free public service |

Open-Meteo's free API is meant for personal, non-commercial use. Commercial use needs your own
Open-Meteo plan, which this plugin doesn't support yet.

## Storm science accuracy

`weather_science` is plain numpy. Its parcel math (Romps LCL, virtual-temperature CAPE/CIN between
the lowest LFC and highest EL), Bunkers storm motion, helicity, shear and precipitable water follow
MetPy 1.7, and ECAPE follows the reference implementation of Peters et al. (2023). The test suite
checks it against values those packages produced for three soundings (a classic supercell sounding
and two real GFS profiles): CAPE and ECAPE within 3%, the rest within about 1%.

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

## License

MIT. See [LICENSE](LICENSE).
