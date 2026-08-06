import { useState, useEffect } from 'react'
import '../styles/TodayBoard.css'

function TodayBoard({ sport, onSelectGame }) {
  const [games, setGames] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

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

  if (loading) return <div className="loading">Loading today's odds...</div>
  if (error) return <div className="error">Error: {error}</div>
  if (games.length === 0) return <div className="empty">No games today. Run `mlb-odds collect --once{sport === 'nfl' ? ' --sport nfl' : ''}` first.</div>

  return (
    <div className="today-board">
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
