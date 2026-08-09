import { useState, useEffect } from 'react'
import LineMovement from './LineMovement'
import '../styles/GameHistory.css'

const statFmt = (v, d = 3) => (v == null ? '–' : v.toFixed(d))

function MatchupCard({ scout }) {
  if (!scout) return null
  const side = (label, team, batting, pitcher, line) => (
    <div className="scout-side">
      <div className="scout-team">{team} <span className="scout-label">{label}</span></div>
      <table className="scout-table">
        <tbody>
          <tr><td>Team batting</td>
            <td>xwOBA <b>{statFmt(batting?.xwoba)}</b></td>
            <td>xBA {statFmt(batting?.xba)}</td>
            <td>xSLG {statFmt(batting?.xslg)}</td></tr>
          <tr><td>Starter</td>
            <td colSpan="3">{pitcher || 'TBD'}{line && (
              <span> — xERA <b>{statFmt(line.xera, 2)}</b> · xwOBA against {statFmt(line.xwoba)} ({line.pa} PA)</span>
            )}</td></tr>
        </tbody>
      </table>
    </div>
  )
  return (
    <div className="matchup-card">
      {side('away', scout.away_team, scout.away_batting, scout.away_pitcher, scout.away_pitcher_line)}
      {side('home', scout.home_team, scout.home_batting, scout.home_pitcher, scout.home_pitcher_line)}
      <div className="scout-note">Statcast expected stats (Baseball Savant) — luck-stripped quality; xwOBA gaps vs actual results are regression candidates.</div>
    </div>
  )
}

function GameHistory({ sport, gameId }) {
  const [history, setHistory] = useState(null)
  const [scout, setScout] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!gameId) return

    const fetchHistory = async () => {
      try {
        setLoading(true)
        const response = await fetch(`/api/games/${gameId}/history?sport=${sport}`)
        if (!response.ok) throw new Error('Failed to fetch game history')
        const data = await response.json()
        setHistory(data)
        setError(null)
      } catch (err) {
        setError(err.message)
        setHistory(null)
      } finally {
        setLoading(false)
      }
    }

    fetchHistory()
    setScout(null)
    if (sport === 'mlb') {
      fetch(`/api/games/${gameId}/scout?sport=mlb`)
        .then((r) => (r.ok ? r.json() : null))
        .then(setScout)
        .catch(() => setScout(null))
    }
  }, [gameId, sport])

  if (!gameId) {
    return <div className="empty">Select a game to view line movement history</div>
  }

  if (loading) return <div className="loading">Loading history...</div>
  if (error) return <div className="error">Error: {error}</div>
  if (!history) return <div className="empty">No history found for this game</div>

  return (
    <div className="game-history">
      <h2>{gameId}</h2>
      <div className="history-meta">{history.count} records</div>

      <MatchupCard scout={scout} />

      <LineMovement rows={history.rows} />

      <div className="history-table-container">
        <table className="history-table">
          <thead>
            <tr>
              <th>Fetched At</th>
              <th>Book</th>
              <th>Market</th>
              <th>Outcome</th>
              <th>Price</th>
              <th>Line</th>
            </tr>
          </thead>
          <tbody>
            {history.rows.map((row, idx) => (
              <tr key={idx}>
                <td>{new Date(row.fetched_at).toLocaleString()}</td>
                <td>{row.book}</td>
                <td>{row.market}</td>
                <td>{row.outcome}</td>
                <td className="price">{row.price > 0 ? '+' : ''}{row.price}</td>
                <td>{row.line ?? '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default GameHistory
