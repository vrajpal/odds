import { useState, useEffect } from 'react'
import LineMovement from './LineMovement'
import '../styles/GameHistory.css'

function GameHistory({ sport, gameId }) {
  const [history, setHistory] = useState(null)
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
