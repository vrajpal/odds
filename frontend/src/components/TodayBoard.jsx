import { useState, useEffect } from 'react'
import '../styles/TodayBoard.css'

function TodayBoard({ sport, onSelectGame }) {
  const [games, setGames] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [pulling, setPulling] = useState(false)
  const [pullNote, setPullNote] = useState(null)

  const pullNow = async () => {
    setPulling(true)
    setPullNote(null)
    try {
      const resp = await fetch(`/api/refresh?sport=${sport}`, { method: 'POST' })
      const body = await resp.json()
      if (!resp.ok) throw new Error(body.detail || 'refresh failed')
      setPullNote(`pulled ${body.games} games (${body.rows} rows)`)
      const r = await fetch(`/api/today?sport=${sport}`)
      if (r.ok) setGames(await r.json())
    } catch (err) {
      setPullNote(err.message)
    } finally {
      setPulling(false)
    }
  }

  useEffect(() => {
    const fetchToday = async () => {
      try {
        setLoading(true)
        const response = await fetch(`/api/today?sport=${sport}`)
        if (!response.ok) throw new Error('Failed to fetch today\'s odds')
        const data = await response.json()
        setGames(data)
        setError(null)
      } catch (err) {
        setError(err.message)
        setGames([])
      } finally {
        setLoading(false)
      }
    }

    fetchToday()
    const interval = setInterval(fetchToday, 30000)
    return () => clearInterval(interval)
  }, [sport])

  const pullButton = (
    <div className="pull-bar">
      <button className="pull-btn" onClick={pullNow} disabled={pulling}>
        {pulling ? 'pulling…' : '↻ pull latest lines (free)'}
      </button>
      {pullNote && <span className="pull-note">{pullNote}</span>}
    </div>
  )

  if (loading) return <div className="loading">Loading today's odds...</div>
  if (error) return <div className="error">Error: {error}</div>
  if (games.length === 0) return (
    <div>
      {pullButton}
      <div className="empty">No games today — try pulling, or the daily cron will populate the next slate.</div>
    </div>
  )

  return (
    <div className="today-board">
      {pullButton}
      {games.map(gameBoard => (
        <div key={gameBoard.game.game_id} className="game-card" onClick={() => onSelectGame(gameBoard.game.game_id)}>
          <div className="game-header">
            <div className="matchup">
              <span className="team away">{gameBoard.game.away_team}</span>
              <span className="vs">@</span>
              <span className="team home">{gameBoard.game.home_team}</span>
            </div>
            <div className="start-time">
              {new Date(gameBoard.game.start_time).toLocaleTimeString([], {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                timeZoneName: 'short'
              })}
            </div>
          </div>

          <table className="odds-table">
            <thead>
              <tr>
                <th>Book</th>
                <th>Moneyline</th>
                <th>{sport === 'nfl' ? 'Spread' : 'Run Line'}</th>
                <th>Total</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(gameBoard.books).map(([book, odds]) => (
                <tr key={book}>
                  <td className="book-name">{book}</td>
                  <td>{odds.moneyline}</td>
                  <td>{sport === 'nfl' ? odds.spread : odds.run_line}</td>
                  <td>{odds.total}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="click-hint">Click to view line movement</div>
        </div>
      ))}
    </div>
  )
}

export default TodayBoard
