import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import './index.css'
import Landing from './pages/Landing.jsx'
import SaleDetail from './pages/SaleDetail.jsx'
import EngineRoom from './pages/EngineRoom.jsx'
import Architecture from './pages/Architecture.jsx'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/sale/:saleId" element={<SaleDetail />} />
        <Route path="/engines" element={<EngineRoom />} />
        <Route path="/architecture" element={<Architecture />} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
)
