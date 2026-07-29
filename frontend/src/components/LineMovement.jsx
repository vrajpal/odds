import { useEffect, useMemo, useRef, useState } from 'react'
import '../styles/LineMovement.css'

const MARKETS = [
  { key: 'moneyline', label: 'Moneyline', outcomes: ['home', 'away'], hasLine: false },
  { key: 'run_line', label: 'Run line', outcomes: ['home', 'away'], hasLine: true },
  { key: 'total', label: 'Total', outcomes: ['over', 'under'], hasLine: true },
]
const OUTCOME_LABELS = { home: 'Home', away: 'Away', over: 'Over', under: 'Under' }
const MAX_SERIES = 8
const DAY_MS = 24 * 60 * 60 * 1000

const fmtSigned = (v) => (v > 0 ? `+${v}` : `${v}`)

function fmtClock(t) {
  return new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function fmtTick(t, spanMs) {
  const d = new Date(t)
  return spanMs <= DAY_MS ? fmtClock(t) : `${d.getMonth() + 1}/${d.getDate()} ${fmtClock(t)}`
}

function niceStep(rough) {
  const mag = 10 ** Math.floor(Math.log10(rough))
  const norm = rough / mag
  if (norm > 5) return 10 * mag
  if (norm > 2) return 5 * mag
  if (norm > 1) return 2 * mag
  return mag
}

function yTicks(min, max, target = 5) {
  const step = niceStep((max - min) / target || 1)
  const ticks = []
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) {
    ticks.push(Math.round(v * 100) / 100)
  }
  return ticks
}

function useContainerWidth() {
  const ref = useRef(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => setWidth(entries[0].contentRect.width))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, width]
}

function MovementChart({ series, formatValue, formatPoint }) {
  const [ref, width] = useContainerWidth()
  const [hoverT, setHoverT] = useState(null)

  const height = 320
  const margin = {
    top: 16,
    right: series.length >= 2 && series.length <= 4 ? 96 : 20,
    bottom: 30,
    left: 56,
  }
  const innerW = Math.max(0, width - margin.left - margin.right)
  const innerH = height - margin.top - margin.bottom

  const { times, t0, span, vMin, vMax } = useMemo(() => {
    const pts = series.flatMap((s) => s.points)
    const ts = [...new Set(pts.map((p) => p.t))].sort((a, b) => a - b)
    const first = ts[0] ?? 0
    const spanMs = (ts[ts.length - 1] ?? 0) - first
    let lo = Math.min(...pts.map((p) => p.v))
    let hi = Math.max(...pts.map((p) => p.v))
    const pad = (hi - lo) * 0.08 || 1
    lo -= pad
    hi += pad
    return { times: ts, t0: first, span: spanMs, vMin: lo, vMax: hi }
  }, [series])

  const x = (t) => (span > 0 ? margin.left + ((t - t0) / span) * innerW : margin.left + innerW / 2)
  const y = (v) => margin.top + (1 - (v - vMin) / (vMax - vMin)) * innerH

  const xTickTimes = useMemo(() => {
    if (times.length <= 7) return times
    const k = 6
    const picked = new Set()
    for (let i = 0; i < k; i++) picked.add(times[Math.round((i * (times.length - 1)) / (k - 1))])
    return [...picked]
  }, [times])

  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left
    let best = null
    for (const t of times) {
      const d = Math.abs(x(t) - px)
      if (!best || d < best.d) best = { t, d }
    }
    setHoverT(best ? best.t : null)
  }

  const hoverRows = hoverT === null
    ? []
    : series
        .map((s) => ({ ...s, point: s.points.find((p) => p.t === hoverT) }))
        .filter((s) => s.point)
        .sort((a, b) => b.point.v - a.point.v)

  // End-of-line labels for up to 4 series, nudged apart so they never overlap.
  const endLabels = useMemo(() => {
    if (series.length < 2 || series.length > 4) return []
    const labels = series
      .map((s) => {
        const last = s.points[s.points.length - 1]
        return { name: s.name, color: s.color, x: x(last.t), y: y(last.v) }
      })
      .sort((a, b) => a.y - b.y)
    for (let i = 1; i < labels.length; i++) {
      if (labels[i].y - labels[i - 1].y < 14) labels[i].y = labels[i - 1].y + 14
    }
    return labels
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series, width, vMin, vMax, t0, span])

  const tooltipLeft = hoverT === null ? 0 : Math.min(x(hoverT) + 12, Math.max(0, width - 200))

  return (
    <div className="movement-plot" ref={ref}>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          onMouseMove={onMove}
          onMouseLeave={() => setHoverT(null)}
        >
          {yTicks(vMin, vMax).map((v) => (
            <g key={v}>
              <line
                x1={margin.left}
                x2={width - margin.right}
                y1={y(v)}
                y2={y(v)}
                className="grid-line"
              />
              <text x={margin.left - 8} y={y(v) + 3.5} className="tick-label" textAnchor="end">
                {formatValue(v)}
              </text>
            </g>
          ))}
          <line
            x1={margin.left}
            x2={width - margin.right}
            y1={height - margin.bottom}
            y2={height - margin.bottom}
            className="axis-line"
          />
          {xTickTimes.map((t) => (
            <text
              key={t}
              x={x(t)}
              y={height - margin.bottom + 18}
              className="tick-label"
              textAnchor="middle"
            >
              {fmtTick(t, span)}
            </text>
          ))}

          {hoverT !== null && (
            <line
              x1={x(hoverT)}
              x2={x(hoverT)}
              y1={margin.top}
              y2={height - margin.bottom}
              className="crosshair"
            />
          )}

          {series.map((s) => (
            <g key={s.name}>
              {s.points.length > 1 && (
                <path
                  d={s.points.map((p, i) => `${i ? 'L' : 'M'}${x(p.t)},${y(p.v)}`).join('')}
                  className="series-line"
                  style={{ stroke: s.color }}
                />
              )}
              {s.points.map((p) => (
                <circle
                  key={p.t}
                  cx={x(p.t)}
                  cy={y(p.v)}
                  r={p.t === hoverT ? 5 : 3}
                  className="series-dot"
                  style={{ fill: s.color }}
                />
              ))}
            </g>
          ))}

          {endLabels.map((l) => (
            <text key={l.name} x={l.x + 8} y={l.y + 3.5} className="end-label">
              {l.name}
            </text>
          ))}
        </svg>
      )}

      {hoverRows.length > 0 && (
        <div className="movement-tooltip" style={{ left: tooltipLeft, top: margin.top }}>
          <div className="tooltip-time">
            {new Date(hoverT).toLocaleString([], {
              month: 'short',
              day: 'numeric',
              hour: 'numeric',
              minute: '2-digit',
            })}
          </div>
          {hoverRows.map((s) => (
            <div key={s.name} className="tooltip-row">
              <span className="tooltip-swatch" style={{ background: s.color }} />
              <span className="tooltip-book">{s.name}</span>
              <span className="tooltip-value">{formatPoint(s.point)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function LineMovement({ rows }) {
  const [marketKey, setMarketKey] = useState(null)
  const [outcomeKey, setOutcomeKey] = useState(null)
  const [metric, setMetric] = useState('price')
  const [hidden, setHidden] = useState([])

  const availableMarkets = useMemo(
    () => MARKETS.filter((m) => rows.some((r) => r.market === m.key)),
    [rows]
  )

  // Slots come from the alphabetical book list across the whole game, so a
  // book keeps its color no matter which market/outcome/toggle is active.
  const bookSlots = useMemo(() => {
    const books = [...new Set(rows.map((r) => r.book))].sort()
    return new Map(books.map((b, i) => [b, i + 1]))
  }, [rows])

  const market = availableMarkets.find((m) => m.key === marketKey) ?? availableMarkets[0]
  if (!market) return null

  const outcome = market.outcomes.includes(outcomeKey) ? outcomeKey : market.outcomes[0]
  const effectiveMetric = market.hasLine ? metric : 'price'

  const filtered = rows.filter((r) => r.market === market.key && r.outcome === outcome)
  const books = [...new Set(filtered.map((r) => r.book))]
    .sort()
    .filter((b) => bookSlots.get(b) <= MAX_SERIES)
  const droppedBooks = new Set(filtered.map((r) => r.book)).size - books.length

  const fmtValue = (v) =>
    effectiveMetric === 'price' || market.key === 'run_line' ? fmtSigned(v) : `${v}`
  const fmtPoint = (p) =>
    effectiveMetric === 'price' && p.line != null
      ? `${market.key === 'run_line' ? fmtSigned(p.line) : p.line} · ${fmtSigned(p.v)}`
      : fmtValue(p.v)

  const series = books
    .filter((b) => !hidden.includes(b))
    .map((book) => {
      const points = filtered
        .filter((r) => r.book === book)
        .map((r) => ({
          t: new Date(r.fetched_at).getTime(),
          v: effectiveMetric === 'price' ? r.price : r.line,
          line: r.line,
        }))
        .filter((p) => p.v != null)
        .sort((a, b) => a.t - b.t)
        // Two providers reporting the same book at the same instant would
        // double-draw every mark; keep the last row per timestamp.
        .filter((p, i, arr) => i === arr.length - 1 || arr[i + 1].t !== p.t)
      return { name: book, color: `var(--series-${bookSlots.get(book)})`, points }
    })
    .filter((s) => s.points.length > 0)

  const toggleBook = (book) =>
    setHidden((h) => (h.includes(book) ? h.filter((b) => b !== book) : [...h, book]))

  return (
    <div className="line-movement">
      <div className="movement-filters">
        <div className="segmented">
          {availableMarkets.map((m) => (
            <button
              key={m.key}
              className={m.key === market.key ? 'active' : ''}
              onClick={() => setMarketKey(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
        <div className="segmented">
          {market.outcomes.map((o) => (
            <button
              key={o}
              className={o === outcome ? 'active' : ''}
              onClick={() => setOutcomeKey(o)}
            >
              {OUTCOME_LABELS[o]}
            </button>
          ))}
        </div>
        {market.hasLine && (
          <div className="segmented">
            <button
              className={effectiveMetric === 'price' ? 'active' : ''}
              onClick={() => setMetric('price')}
            >
              Price
            </button>
            <button
              className={effectiveMetric === 'line' ? 'active' : ''}
              onClick={() => setMetric('line')}
            >
              Line
            </button>
          </div>
        )}
      </div>

      {books.length > 1 && (
        <div className="movement-legend">
          {books.map((book) => (
            <button
              key={book}
              className={`legend-chip ${hidden.includes(book) ? 'off' : ''}`}
              onClick={() => toggleBook(book)}
            >
              <span
                className="legend-swatch"
                style={{ background: `var(--series-${bookSlots.get(book)})` }}
              />
              {book}
            </button>
          ))}
          {droppedBooks > 0 && (
            <span className="legend-note">
              showing {books.length} of {books.length + droppedBooks} books
            </span>
          )}
        </div>
      )}

      {series.length > 0 ? (
        <MovementChart series={series} formatValue={fmtValue} formatPoint={fmtPoint} />
      ) : (
        <div className="movement-empty">No data for this selection</div>
      )}
    </div>
  )
}

export default LineMovement
