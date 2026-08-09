import { useState, useEffect, useCallback } from 'react'
import '../styles/Dashboard.css'

const pct = (p) => (p == null ? '–' : `${(p * 100).toFixed(1)}%`)
const pp = (p) => (p == null ? '–' : `${p > 0 ? '+' : ''}${(p * 100).toFixed(1)}pp`)
const signed = (v) => (v > 0 ? `+${v}` : `${v}`)

function evBadge(side) {
  if (!side) return '–'
  const cls = side.ev > 0.005 ? 'ev-pos' : side.ev < -0.03 ? 'ev-neg' : 'ev-flat'
  const star = side.model_ev != null && side.model_ev > 0.02
  return (
    <span title={side.model_ev != null ? `model EV ${(side.model_ev * 100).toFixed(1)}%` : undefined}>
      <b>{signed(side.price)}</b> <span className="book">{side.book}</span>{' '}
      <span className={`ev ${cls}`}>{(side.ev * 100).toFixed(1)}%</span>
      {star && <span className="model-star" title={`model EV +${(side.model_ev * 100).toFixed(1)}% — model likes this price`}>★</span>}
    </span>
  )
}

function marketCell(books, kind) {
  const entries = Object.entries(books)
  if (!entries.length) return '–'
  if (kind === 'run_line') {
    const best = entries.reduce((a, b) => ((b[1].home ?? -999) > (a[1].home ?? -999) ? b : a))
    const [book, q] = best
    return q.line == null ? '–' : (
      <span>{q.line > 0 ? `+${q.line}` : q.line} <b>{signed(q.home ?? 0)}</b>{' '}
        <span className="book">{book}</span></span>
    )
  }
  const best = entries.reduce((a, b) => ((b[1].over ?? -999) > (a[1].over ?? -999) ? b : a))
  const [book, q] = best
  return q.line == null ? '–' : (
    <span>{q.line} <b>o{signed(q.over ?? 0)}</b> <span className="book">{book}</span></span>
  )
}

function Dashboard({ sport, onSelectGame }) {
  const [date, setDate] = useState(null) // null = today (server decides)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [pulling, setPulling] = useState(false)

  const load = useCallback(async (d) => {
    try {
      const qs = new URLSearchParams({ sport, ...(d ? { on: d } : {}) })
      const resp = await fetch(`/api/dashboard?${qs}`)
      if (!resp.ok) throw new Error((await resp.json()).detail || 'failed to load')
      setData(await resp.json())
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [sport])

  useEffect(() => { load(date) }, [load, date])

  const shiftDay = (delta) => {
    const base = new Date((date || data?.date || new Date().toISOString().slice(0, 10)) + 'T12:00:00')
    base.setDate(base.getDate() + delta)
    setDate(base.toISOString().slice(0, 10))
  }

  const pullNow = async () => {
    setPulling(true)
    try {
      await fetch(`/api/refresh?sport=${sport}`, { method: 'POST' })
      await load(date)
    } finally {
      setPulling(false)
    }
  }

  if (error) return <div className="error">Error: {error}</div>
  if (!data) return <div className="loading">Loading dashboard…</div>

  return (
    <div className="dashboard">
      <div className="dash-controls">
        <button onClick={() => shiftDay(-1)}>‹</button>
        <input type="date" value={date || data.date}
               onChange={(e) => setDate(e.target.value)} />
        <button onClick={() => shiftDay(1)}>›</button>
        {date && <button className="today-btn" onClick={() => setDate(null)}>today</button>}
        <button className="pull-btn" onClick={pullNow} disabled={pulling}>
          {pulling ? 'pulling…' : '↻ pull latest'}
        </button>
      </div>

      {data.games.length === 0 ? (
        <div className="empty">No games stored for {data.date}. Pull latest, or pick another day.</div>
      ) : (
        <div className="dash-scroll">
          <table className="dash-table">
            <thead>
              <tr>
                <th>Time</th><th>Matchup</th>
                <th>Away ML (best)</th><th>Home ML (best)</th>
                <th>Cons %</th><th>Model %</th>{sport === 'nfl' && <th>Margin</th>}<th>Edge</th><th>Drift</th>
                <th>{sport === 'nfl' ? 'Spread' : 'Run line'}</th><th>Total</th>
              </tr>
            </thead>
            <tbody>
              {data.games.map((g) => {
                const ml = g.moneyline
                const edgeCls = ml.model_edge > 0.02 ? 'ev-pos' : ml.model_edge < -0.02 ? 'ev-neg' : ''
                return (
                  <tr key={g.game_id} onClick={() => onSelectGame(g.game_id)}
                      title="click for line movement">
                    <td className="dim">
                      {new Date(g.start_time).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                    </td>
                    <td className="matchup">{g.away_team} @ {g.home_team}</td>
                    <td>{evBadge(ml.best_away)}</td>
                    <td>{evBadge(ml.best_home)}</td>
                    <td>{pct(ml.consensus_prob)}</td>
                    <td title={(sport === 'nfl'
                        ? `moneyline lens ${pct(ml.market_model_prob)} · spread lens ${pct(ml.spread_model_prob)}`
                        : `market model ${pct(ml.market_model_prob)} · statcast ${pct(ml.statcast_prob)}`)
                        + (ml.projection_prob != null ? ` · projection ${pct(ml.projection_prob)} (${ml.projection_source})` : '')}>
                      {pct(ml.model_prob)}
                    </td>
                    {sport === 'nfl' && (
                      <td className="num">
                        {g.predicted_margin == null ? '–'
                          : g.predicted_margin > 0
                            ? `${g.home_team} by ${g.predicted_margin.toFixed(1)}`
                            : `${g.away_team} by ${(-g.predicted_margin).toFixed(1)}`}
                      </td>
                    )}
                    <td className={edgeCls}>{pp(ml.model_edge)}</td>
                    <td className={ml.drift > 0.01 ? 'ev-pos' : ml.drift < -0.01 ? 'ev-neg' : ''}>
                      {pp(ml.drift)}
                    </td>
                    <td>{marketCell(g.run_line, 'run_line')}</td>
                    <td>{marketCell(g.total, 'total')}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="dash-legend">
        Home-win probabilities, de-vigged. <b>Cons</b> = market median · <b>Model</b> = market-implied
        team strengths blended 70/30 with Statcast (team xwOBA + probable starters; hover for
        components){data.hfa != null && ` — HFA ${data.hfa > 0 ? '+' : ''}${data.hfa} log-odds`} ·
        <b> Edge</b> = model − consensus · <b>Drift</b> = consensus now vs first snapshot ·
        EV% next to each price is vs the consensus fair line; a <span className="model-star">★</span> marks
        prices the model itself rates +2% EV or better{sport === 'nfl' && ' · Margin = spread-ratings predicted result'}.
        Click a game for full line movement.
      </div>

      {data.strengths.length > 0 && (
        <details className="strengths">
          <summary>Market-implied team strengths ({data.strengths.length})</summary>
          <table className="dash-table slim">
            <thead><tr><th>#</th><th>team</th><th>strength (log-odds)</th></tr></thead>
            <tbody>
              {data.strengths.map((s, i) => (
                <tr key={s.team}>
                  <td className="dim">{i + 1}</td>
                  <td>{s.team}</td>
                  <td className={s.strength > 0 ? 'ev-pos' : 'ev-neg'}>
                    {s.strength > 0 ? '+' : ''}{s.strength.toFixed(3)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  )
}

export default Dashboard
