---
name: weather
description: How to answer weather questions with Better Weather — which tool to use, how to talk about alerts safely, and how to handle home and saved places.
version: 0.1.0
metadata:
  hermes:
    tags: [weather, forecast, alerts, severe-weather]
    category: weather
---

# Better Weather

Five tools. Start light, go deeper only when asked.

| The user asks… | Use |
|---|---|
| "How's the weather?", "Is it raining?", "Jacket?" | `weather` (no arguments = home) |
| "This weekend?", "Next week?", "Hour by hour?", "When does the rain start?" | `weather_forecast` (`hourly: true` for hours) |
| "Any warnings?", details of an alert `weather` mentioned | `weather_alerts` |
| "Storms coming?", "Tornado risk?", "Severe weather today?" | `weather_severe` |
| "Home is Ankeny", "Save my mom's place as Ames" | `weather_places` |

## Places
- No location means home. If the tool says no home is set, ask the user where home is and call
  `weather_places` with `action: "set_home"`. Don't guess their town.
- Place names work best as "City, ST" in the US or "City, Country" elsewhere. ZIP codes, airport
  codes (KDSM) and "lat,lon" work too.
- If a result has a `note` saying the match was approximate, tell the user which place you used.

## Alerts and safety (always)
- Every answer that lists an alert must mention it, even if the user asked about something else.
- `life_threatening: true` (tornado, flash flood, extreme wind, hurricane, storm surge warnings)
  goes FIRST, in plain words, with the NWS instructions quoted. Never soften, summarize away, or
  joke about a warning.
- If a note says alerts couldn't be checked, say so. Never say "no alerts" in that case.
- The NWS is the official source in the US. You're relaying it, not replacing it.

## Answer style
- Lead with what matters: temperature and sky now, then what's coming, then alerts.
- Mention the source and time once ("KDSM report from 3:35 PM"). If a note says data is stale or
  came from a backup source, say that in a few words.
- Use the units the tool returned; don't convert unless the user asks.
- Outside the US there are no official alerts yet. Say so if the user asks about warnings abroad.

## /weather
Users can type `/weather`, `/weather forecast`, `/weather hourly`, `/weather alerts` or
`/weather severe` (each optionally followed by a place) to get an answer without the AI.
