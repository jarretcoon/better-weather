---
name: weather
description: How to answer weather questions with Better Weather — which tool to use, how to show radar and satellite pictures, how to talk about alerts safely, and how to set up warning pushes.
version: 0.3.0
metadata:
  hermes:
    tags: [weather, forecast, alerts, severe-weather, radar, satellite]
    category: weather
---

# Better Weather

Sixteen tools. Start light, go deeper only when asked.

| The user asks… | Use |
|---|---|
| "How's the weather?", "Is it raining?", "Jacket?" | `weather` (no arguments = home) |
| "This weekend?", "Next week?", "Hour by hour?", "When does the rain start?" | `weather_forecast` (`hourly: true` for hours) |
| "Any warnings?", details of an alert `weather` mentioned | `weather_alerts` |
| "Storms coming?", "Tornado risk?", "Severe weather today?" | `weather_severe` |
| "Show me the radar", "Is that storm close?" | `weather_radar` (`loop: true` for motion) |
| "Show me the clouds / satellite / smoke from above" | `weather_satellite` |
| "Is the air OK?", "Smoke?", "UV?", "Pollen?" | `weather_air` |
| "How cold was it yesterday?", "Normal for today?", "Record high?" | `weather_history` |
| "How sure are they?", "Will it really snow?", "How much rain could we get?" | `weather_confidence` |
| "Why?", "What are the forecasters thinking?" | `weather_discussion` (summarize it plainly) |
| "When should I run / mow / grill / wash the car?", "Good night for stars?" | `weather_best_time` |
| "Which forecast should I trust?", "Who's been right lately?" | `weather_scorecard` |
| "Flying today?", "Delays at O'Hare?", "Weather when I land in Denver?" | `weather_travel` |
| "Is the river flooding?", "Tides?", "Waves on the lake?" | `weather_water` |
| "How unstable is it?", "CAPE?", "Tornado ingredients?", "Show me the sounding numbers" | `weather_science` (`at_peak: true` on storm days) |
| "Home is Ankeny", "Save my mom's place as Ames" | `weather_places` |

## Pictures (radar, satellite)
- The result has a `media` field like `MEDIA:/…/radar-KDMX-0.gif`. Put that exact line on its own
  line in your reply, not in a code block. The chat turns it into the picture.
- Say what the picture is (source and time) in one short line. Don't describe pixels you can't see.
- For a storm the user is worried about, offer `loop: true` (shows motion) and `weather_severe`.
- Radar is US-only. Satellite covers the US and nearby. Say so if asked about elsewhere.

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

## Warning pushes (zero tokens)
If the user wants warnings pushed to them automatically:
1. Run `hermes better-weather push setup` in the terminal (add a place, `--level warnings|watches|all`,
   `--freeze` for a 3 PM freeze heads-up, `--rain` for rain-within-the-hour heads-ups).
2. If no `better-weather-alerts` cron job exists yet, create one with the cron tool: schedule
   `every 2m`, `no_agent: true`, script `better-weather-alerts.py`, deliver to the user's chat.
   The script prints only when something is new, so quiet ticks send nothing and cost nothing.
3. `hermes better-weather push check` shows what a run would send without sending it.

Scheduled briefs with the AI (morning brief, week ahead, weekend) are blueprints:
`/blueprint better-weather:morning-brief` and friends.

## Confidence and the scorecard
- For anything 3+ days out, a quick `weather_confidence` check keeps you honest: say "about 60% of
  model runs show rain" rather than a flat yes or no, and give highs as a range when confidence is low.
- The scorecard needs about two weeks of daily checks before it names a most-accurate source. Until
  then, say it's still learning.

## Answer style
- Lead with what matters: temperature and sky now, then what's coming, then alerts.
- Mention the source and time once ("KDSM report from 3:35 PM"). If a note says data is stale or
  came from a backup source, say that in a few words.
- History comes from the ERA5 reanalysis, not official station records; say "about" for records.
- Use the units the tool returned; don't convert unless the user asks.

## /weather
`/weather`, `/weather forecast`, `/weather hourly`, `/weather alerts`, `/weather severe`,
`/weather radar [loop]`, `/weather satellite [loop]`, `/weather air`, `/weather history [date]`
(each optionally followed by a place) answer without the AI.
