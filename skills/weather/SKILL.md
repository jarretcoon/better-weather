---
name: weather
description: How to answer weather questions with Better Weather — which tool to use, how to send its chat cards and radar/satellite pictures, how to talk about alerts safely, and how to set up warning pushes.
version: 0.9.4
metadata:
  hermes:
    tags: [weather, forecast, alerts, severe-weather, radar, satellite]
    category: weather
---

# Better Weather

## Send the card
`weather`, `weather_forecast` and `weather_alerts` return a `card`: a short emoji block made for chat.
**Send it as-is**, then at most one line of your own (advice, a caveat from `notes`). If the user asked
something specific ("jacket?"), answer that in one line first, then the card. Don't rewrite it into
paragraphs or a table, and don't repeat what it says.

## Which tool
| The user asks… | Use |
|---|---|
| Weather now, "is it raining?", "jacket?" | `weather` (no location = home) |
| Sunrise, sunset, the moon tonight | `weather` (its card and its `sun` and `moon`) |
| Weekend, next week, hour by hour, "when does the rain start?" | `weather_forecast` (`hourly: true`) |
| "Any warnings?", details of an alert | `weather_alerts` |
| Storms, tornado risk, "severe weather today?" | `weather_severe` |
| Radar, "is that storm close?" | `weather_radar` (`loop: true` for motion) |
| Clouds, satellite, smoke from above | `weather_satellite` |
| Air quality, smoke, UV, pollen | `weather_air` |
| Yesterday, normals, records | `weather_history` |
| "How sure are they?", "how much rain could we get?" | `weather_confidence` |
| "Why?", what forecasters think | `weather_discussion` (summarize plainly) |
| Best time to run, mow, grill, wash the car, stargaze | `weather_best_time` |
| "Which forecast should I trust?" | `weather_scorecard` |
| Flights, airport delays, weather on arrival | `weather_travel` |
| Rivers, tides, waves | `weather_water` (tides: `next` are still to come, `last` has passed) |
| CAPE, shear, tornado ingredients | `weather_science` (`at_peak: true` on storm days) |
| "Home is Ankeny", "save Mom's place as Ames" | `weather_places` |

## Alerts and safety (always)
- Any alert in a result gets mentioned, even if the user asked about something else.
- `life_threatening: true` (tornado, flash flood, extreme wind, hurricane, storm surge) goes FIRST,
  in plain words, with the instructions quoted. Never soften, summarize away or joke about a warning.
- A ⚠️ note saying alerts weren't or couldn't be checked: say so. Never say "no alerts" then.
- The NWS (and Environment Canada, MeteoAlarm) is the official source; you're relaying it.

## Pictures
- Radar and satellite results have a `media` line like `MEDIA:/…/radar-KDMX-0.gif`. Put that exact
  line on its own line, not in a code block; the chat shows the picture. Add one line on what it is.
- Radar is US-only; satellite covers the US and nearby.

## Places
- No home set: ask where home is, then `weather_places` `action: "set_home"`. Don't guess.
- "City, ST", "City, Country", ZIP, airport code (DSM or KDSM), "lat,lon" and shorthands like LA, NYC
  or DC all work. A state on its own isn't a place: when the tool asks which place in it, ask the user.
- If a `note` says the match was approximate or another place has the same name, say which place
  you used.

## Honesty
- 3+ days out, `weather_confidence` keeps you honest: "about 60% of model runs show rain", ranges
  when confidence is low.
- US history is NOAA station records; elsewhere it's modelled (the ERA5 reanalysis, and Open-Meteo's
  forecast models for the last few days), so say "about".
- The scorecard needs about two weeks of data before it names a winner.
- Mention a stale or backup source in a few words when a note says so. Keep the units returned.

## Warning pushes (zero tokens)
1. `hermes better-weather push setup [place] --level warnings|watches|all [--freeze] [--rain]`
2. If there's no `better-weather-alerts` cron job, create one: `every 2m`, `no_agent: true`, script
   `better-weather-alerts.py`, deliver to the user's chat. Quiet ticks send nothing.
3. `hermes better-weather push check` previews a run without sending.

Briefs with the AI: `/blueprint better-weather:morning-brief`, `:week-ahead`, `:weekend-plan`.

## If asked what Better Weather can do
Answer from this list; don't read the plugin's files.
- Now (with sunrise, sunset and the moon), hourly (72 h) and daily (16 days) for any place;
  official alerts (US, Canada, 38 European countries) and severe-storm risk at the exact spot;
  radar and satellite pictures
- Air quality/smoke/UV/pollen; past weather with normals and records; forecast confidence; the
  forecasters' discussion; which forecast has been most accurate at home
- Best time for outdoor plans; travel (airports, delays, weather on arrival); rivers, tides, waves;
  storm science (CAPE, shear, ECAPE)
- `/weather` commands without the AI, zero-token warning pushes, scheduled briefs

## /weather
`/weather`, `forecast`, `hourly`, `alerts`, `severe`, `radar [loop]`, `satellite [loop]`, `air`,
`history [date]`, each optionally followed by a place. No AI involved.
