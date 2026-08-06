import { useState } from 'react'
import TodayBoard from './components/TodayBoard'
import GameHistory from './components/GameHistory'
import './App.css'

function App() {
  const [activeTab, setActiveTab] = useState('today')
  const [selectedGame, setSelectedGame] = useState(null)
  const [sport, setSport] = useState('mlb')

  const switchSport = (next) => {
    setSport(next)
    setSelectedGame(null)   // game ids are per-sport databases (D-019)
    setActiveTab('today')
  }

  return (
    <div className="app">
      <header className="header">
        <h1>{sport.toUpperCase()} Odds</h1>
        <nav className="nav sport-switch">
          {['mlb', 'nfl'].map(s => (
            <button
              key={s}
              className={`nav-btn ${sport === s ? 'active' : ''}`}
              onClick={() => switchSport(s)}
            >
              {s.toUpperCase()}
            </button>
          ))}
        </nav>
        <nav className="nav">
          <button
            className={`nav-btn ${activeTab === 'today' ? 'active' : ''}`}
            onClick={() => { setActiveTab('today'); setSelectedGame(null) }}
          >
            Today
          </button>
          <button
            className={`nav-btn ${activeTab === 'history' ? 'active' : ''}`}
            onClick={() => setActiveTab('history')}
          >
            History
          </button>
        </nav>
      </header>

      <main className="main">
        {activeTab === 'today' && (
          <TodayBoard sport={sport} onSelectGame={(gameId) => {
            setSelectedGame(gameId)
            setActiveTab('history')
          }} />
        )}
        {activeTab === 'history' && (
          <GameHistory sport={sport} gameId={selectedGame} />
        )}
      </main>
    </div>
  )
}

export default App
