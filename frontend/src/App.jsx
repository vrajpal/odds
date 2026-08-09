import { useState } from 'react'
import TodayBoard from './components/TodayBoard'
import Dashboard from './components/Dashboard'
import GameHistory from './components/GameHistory'
import { useTheme } from './theme'
import './App.css'

function App() {
  const [activeTab, setActiveTab] = useState('dashboard')
  const [selectedGame, setSelectedGame] = useState(null)
  const [sport, setSport] = useState('mlb')
  const [theme, toggleTheme] = useTheme()

  const switchSport = (next) => {
    setSport(next)
    setSelectedGame(null)   // game ids are per-sport databases (D-019)
    setActiveTab('today')
  }

  return (
    <div className="app">
      <header className="header">
        <button
          className="theme-toggle"
          onClick={toggleTheme}
          aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
        >
          {theme === 'dark' ? '☀️' : '🌙'}
        </button>
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
            className={`nav-btn ${activeTab === 'dashboard' ? 'active' : ''}`}
            onClick={() => { setActiveTab('dashboard'); setSelectedGame(null) }}
          >
            Dashboard
          </button>
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
            Matchup
          </button>
        </nav>
      </header>

      <main className="main">
        {activeTab === 'dashboard' && (
          <Dashboard sport={sport} onSelectGame={(gameId) => {
            setSelectedGame(gameId)
            setActiveTab('history')
          }} />
        )}
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
