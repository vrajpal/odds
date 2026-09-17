import { useEffect, useMemo, useState } from 'react'
import '../styles/GameMarkets.css'

const pct = (p) => (p == null ? '–' : `${(p * 100).toFixed(1)}%`)
const ev = (v) => (v == null ? '–' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`)
const signed = (v, d = 1) => (v == null ? '–' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}`)
const price = (v) => (v > 0 ? `+${v}` : `${v}`)
const evCls = (v) => (v == null ? 'ev-none' : v > 0.005 ? 'ev-pos' : v < -0.03 ? 'ev-neg' : 'ev-flat')

const GAME_MARKETS = ['moneyline', 'spread', 'run_line', 'total']
const staleAge = (iso) => {
  const h = (Date.now() - new Date(iso)) / 36e5
  return h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`
}
const MARKET_LABEL = { moneyline: 'Moneyline', spread: 'Spread', run_line: 'Run line', total: 'Total', props: 'Props' }

// The situational read: what the market and the model think, and why.
function ContextCard({ data }) {
  const c = data.context
  const home = data.home_team, away = data.away_team
  const marginText = (m) => (m == null ? '–' : m > 0 ? `${home} by ${Math.abs(m).toFixed(1)}` : m < 0 ? `${away} by ${Math.abs(m).toFixed(1)}` : 'pick')
  const lenses = data.sport === 'nfl'
    ? `moneyline lens ${pct(c.market_model_prob)} · spread lens ${pct(c.spread_model_prob)}`
    : `market lens ${pct(c.market_model_prob)} · statcast ${pct(c.statcast_prob)}`
  const flags = []
  if (c.divisional) flags.push({ cls: 'flag-div', text: 'divisional' })
  if (c.home_rest != null && c.home_rest <= 4) flags.push({ cls: 'flag-warn', text: `${home} short week (${c.home_rest}d)` })
  if (c.away_rest != null && c.away_rest <= 4) flags.push({ cls: 'flag-warn', text: `${away} short week (${c.away_rest}d)` })
  if (c.home_rest != null && c.home_rest >= 13) flags.push({ cls: 'flag-good', text: `${home} off a bye` })
  if (c.away_rest != null && c.away_rest >= 13) flags.push({ cls: 'flag-good', text: `${away} off a bye` })
  if (c.rest_differential != null && Math.abs(c.rest_differential) >= 3) {
    flags.push({ cls: 'flag-div', text: `rest edge ${c.rest_differential > 0 ? home : away} +${Math.abs(c.rest_differential)}d` })
  }
  return (
    <div className="gm-context">
      <div className="gm-stats">
        <div className="gm-stat">
          <div className="k">market · {home} win</div>
          <div className="v">{pct(c.consensus_prob)}</div>
          <div className="s">open {pct(c.open_prob)} · drift <span className={c.drift > 0.01 ? 'ev-pos' : c.drift < -0.01 ? 'ev-neg' : ''}>{ev(c.drift)}</span></div>
        </div>
        <div className="gm-stat" title={lenses + (c.projection_prob != null ? ` · projection ${pct(c.projection_prob)} (${c.projection_source})` : '')}>
          <div className="k">model · {home} win</div>
          <div className="v">{pct(c.model_prob)}</div>
          <div className="s">{lenses}</div>
        </div>
        <div className="gm-stat">
          <div className="k">expected margin</div>
          <div className="v">{marginText(c.expected_margin)}</div>
          <div className="s">market · model says {marginText(c.predicted_margin)}</div>
        </div>
        <div className="gm-stat">
          <div className="k">consensus numbers</div>
          <div className="v">{c.consensus_spread == null ? '–' : `${home} ${signed(c.consensus_spread)}`}</div>
          <div className="s">total {c.consensus_total ?? '–'}</div>
        </div>
        <div className="gm-stat">
          <div className="k">coverage</div>
          <div className="v">{c.books} books</div>
          <div className="s">{c.snapshots} snapshots{c.last_seen ? ` · last ${new Date(c.last_seen).toLocaleString([], { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' })}` : ''}</div>
        </div>
      </div>
      {flags.length > 0 && (
        <div className="gm-flags">{flags.map((f) => <span key={f.text} className={`gm-flag ${f.cls}`}>{f.text}</span>)}</div>
      )}
    </div>
  )
}

function GameMarkets({ sport, gameId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [market, setMarket] = useState('all')
  const [sortKey, setSortKey] = useState('ev')
  const [bestOnly, setBestOnly] = useState(false)
  const [hideStale, setHideStale] = useState(true)

  useEffect(() => {
    if (!gameId) return
    setData(null); setError(null)
    fetch(`/api/games/${gameId}/markets?sport=${sport}`)
      .then(async (r) => { if (!r.ok) throw new Error((await r.json()).detail || 'failed to load markets'); return r.json() })
      .then(setData)
      .catch((e) => setError(e.message))
  }, [gameId, sport])

  const available = useMemo(() => {
    if (!data) return []
    const seen = new Set(data.rows.map((r) => (GAME_MARKETS.includes(r.market) ? r.market : 'props')))
    return ['moneyline', 'spread', 'run_line', 'total', 'props'].filter((m) => seen.has(m))
  }, [data])

  const rows = useMemo(() => {
    if (!data) return []
    let out = data.rows.filter((r) => market === 'all' || (market === 'props' ? !GAME_MARKETS.includes(r.market) : r.market === market))
    if (bestOnly) out = out.filter((r) => r.best)
    if (hideStale) out = out.filter((r) => !r.stale)
    const key = { ev: (r) => r.ev, model_ev: (r) => r.model_ev, price: (r) => r.price, book: (r) => r.book, line: (r) => r.line_edge }[sortKey]
    const val = (r) => key(r)
    return [...out].sort((a, b) => {
      const av = val(a), bv = val(b)
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      return typeof av === 'string' ? av.localeCompare(bv) : bv - av
    })
  }, [data, market, sortKey, bestOnly, hideStale])
  const staleCount = data ? data.rows.filter((r) => r.stale).length : 0

  if (!gameId) return null
  if (error) return <div className="gm-error">Markets: {error}</div>
  if (!data) return <div className="gm-loading">Loading markets…</div>

  return (
    <div className="game-markets">
      <ContextCard data={data} />

      <div className="gm-controls">
        <div className="gm-filter">
          <button className={market === 'all' ? 'on' : ''} onClick={() => setMarket('all')}>all ({data.rows.length})</button>
          {available.map((m) => (
            <button key={m} className={market === m ? 'on' : ''} onClick={() => setMarket(m)}>{MARKET_LABEL[m]}</button>
          ))}
        </div>
        <label>sort
          <select value={sortKey} onChange={(e) => setSortKey(e.target.value)}>
            <option value="ev">market EV</option>
            <option value="model_ev">model EV</option>
            <option value="line">line vs consensus</option>
            <option value="price">price</option>
            <option value="book">book</option>
          </select>
        </label>
        <label><input type="checkbox" checked={bestOnly} onChange={(e) => setBestOnly(e.target.checked)} /> best price per side only</label>
        {staleCount > 0 && (
          <label title="a book whose newest quote on this market is more than a day older than the game's newest snapshot — carried forward, not an offer you can take">
            <input type="checkbox" checked={hideStale} onChange={(e) => setHideStale(e.target.checked)} /> hide {staleCount} stale
          </label>
        )}
      </div>

      <div className="gm-scroll">
        <table className="gm-table">
          <thead>
            <tr>
              <th>Bet</th><th>Book</th><th className="num">Price</th><th className="num">Line vs cons</th>
              <th className="num">Fair</th><th className="num">EV</th><th className="num">Model</th><th className="num">Model EV</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.market}-${r.side}-${r.book}-${r.player ?? ''}-${r.line ?? ''}-${i}`} className={r.best ? 'best' : r.stale ? 'stale' : ''}>
                <td className="bet">
                  <span className="mk">{MARKET_LABEL[r.market] ?? r.market.replace(/_/g, ' ')}</span> <b>{r.label}</b>
                  {r.best && <span className="star" title="best price for this side">★</span>}
                  {r.stale && <span className="stale-tag" title={`last quoted ${new Date(r.quoted_at).toLocaleString()}`}>stale {staleAge(r.quoted_at)}</span>}
                </td>
                <td className="book" title={r.quoted_at ? `quoted ${new Date(r.quoted_at).toLocaleString()}` : undefined}>{r.book}</td>
                <td className="num"><b>{price(r.price)}</b></td>
                <td className="num">
                  {r.line_edge == null ? <span className="dim">–</span> : (
                    <span className={r.line_edge > 0 ? 'ev-pos' : r.line_edge < 0 ? 'ev-neg' : 'dim'}>{signed(r.line_edge, 2)}</span>
                  )}
                  {r.key_numbers.length > 0 && <span className="keys" title="crosses a key number vs consensus">{r.key_numbers.map((k) => Math.abs(k)).join(',')}</span>}
                </td>
                <td className="num">{pct(r.fair_prob)}</td>
                <td className={`num ${evCls(r.ev)}`}>{ev(r.ev)}</td>
                <td className="num">{pct(r.model_prob)}</td>
                <td className={`num ${evCls(r.model_ev)}`}>{ev(r.model_ev)}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan="8" className="dim">Nothing quoted for this filter.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="gm-legend">
        Every quote on the game, priced at its own number. <b>Fair</b> = what the market implies for that side at that
        book's line (moneylines: de-vigged consensus; spreads and totals: the normal margin model centred on the market's
        expected margin, so a book hanging a different number is priced for the number it hangs). <b>EV</b> is per unit at the
        book's price. <b>Model</b> repeats the conversion at the model's expected margin; totals have no model view.
        <b> Line vs cons</b> = points this side gets beyond the consensus number; a key-number badge means the book's line and the
        consensus straddle 3 or 7. Props are de-vigged per book and judged against every book quoting the same line.
        <b> Stale</b> rows are a book's last quote from before it stopped reporting on this market (more than a day behind the
        game's newest snapshot): shown for the record, never ranked or starred, hidden by default.
      </div>
    </div>
  )
}

export default GameMarkets
