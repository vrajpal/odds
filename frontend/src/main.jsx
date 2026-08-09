import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { applyTheme } from './theme'
import './index.css'

applyTheme() // set before first paint so the wrong theme never flashes

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
