import { useState, useEffect } from 'react'
import TodayBoard from './components/TodayBoard'
import GameHistory from './components/GameHistory'
import './App.css'

function App() {
  const [activeTab, setActiveTab] = useState('today')
  const [selectedGame, setSelectedGame] = useState(null)

  return (
    <div className="app">
      <header className="header">
        <h1>MLB Odds</h1>
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
          <TodayBoard onSelectGame={(gameId) => {
            setSelectedGame(gameId)
            setActiveTab('history')
          }} />
        )}
        {activeTab === 'history' && (
          <GameHistory gameId={selectedGame} />
        )}
      </main>
    </div>
  )
}

export default App
