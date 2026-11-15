// Better Weather — Hermes Desktop status-bar chip: home temperature, sky, and any active alert.
// Data comes from this plugin's own backend (dashboard/plugin_api.py → GET /api/plugins/better-weather/now).
import { STATUSBAR_AREAS, host } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx } from 'react/jsx-runtime'

const REFRESH_MS = 10 * 60 * 1000

function WeatherChip({ ctx }) {
  const [data, setData] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      ctx
        .rest('/now', { timeoutMs: 45000 })
        .then(d => {
          if (alive) setData(d)
        })
        .catch(() => {
          if (alive) setData({ error: 'Weather backend unavailable' })
        })
    load()
    const stop = ctx.setInterval(load, REFRESH_MS)
    return () => {
      alive = false
      stop()
    }
  }, [])

  if (!data) return null
  const alert = data.alerts && data.alerts.length ? data.alerts[0] : null
  const text = data.error
    ? '🌦 –'
    : `${data.life_threatening ? '🚨' : '🌦'} ${data.temp || '–'}${alert ? ` · ${alert}` : ''}`
  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title: data.error ? data.error : `${data.place}${data.sky ? `: ${data.sky}` : ''} (${data.as_of || ''})`,
    onClick: () => {
      host.notify({ kind: 'info', message: data.error || data.summary || text })
      ctx.rest('/now?refresh=true', { timeoutMs: 45000 }).then(setData).catch(() => {})
    },
    children: text
  })
}

export default {
  id: 'better-weather',
  name: 'Better Weather',
  defaultEnabled: false,
  register(ctx) {
    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => jsx(WeatherChip, { ctx })
    })
  }
}
