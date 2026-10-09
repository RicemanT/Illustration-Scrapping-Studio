import React, { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { ErrorBoundary } from '../components/Feedback';
import Sidebar from '../components/Sidebar';
import { useTouchMode } from './touchMode';

const NAV = [
  ['/review', 'Review queue'],
  ['/dashboard', 'Dashboard'],
  ['/tracker', 'Tracker'],
  ['/planner', 'Dataset planner'],
  ['/settings', 'Settings'],
  ['/logs', 'Logs'],
];

// Phone and tablet shell: a slim top bar and a slide-out menu holding the navigation and the collections list.
export default function TouchLayout() {
  const location = useLocation();
  const [menu, setMenu] = useState(false);
  const { mode, setMode } = useTouchMode();
  useEffect(() => { setMenu(false); }, [location.pathname]);
  const fullScreen = location.pathname.startsWith('/folder/') || location.pathname.startsWith('/collection/') || location.pathname === '/review';
  return (
    <div data-app-shell className="app-atmosphere flex h-[100dvh] flex-col text-slate-300">
      <header className="flex items-center gap-2 border-b border-[#202a34] bg-[#0b1016] px-2" style={{ paddingTop: 'env(safe-area-inset-top, 0px)' }}>
        <button type="button" aria-label="Open menu" aria-expanded={menu} onClick={() => setMenu(true)}
          className="flex h-12 w-12 items-center justify-center rounded text-2xl text-slate-200 active:bg-[#1b2539]">☰</button>
        <Link to="/review" className="flex-1 truncate text-sm font-semibold text-slate-100">Illustration Scrapping Studio</Link>
        <Link to="/review" className="rounded border border-[#2a3a54] px-3 py-2 text-xs text-blue-200 active:bg-[#1b2539]">Review</Link>
      </header>
      <main className={`flex-1 overflow-y-auto overflow-x-auto ${fullScreen ? '' : 'p-2'}`}>
        <ErrorBoundary key={location.pathname}><Outlet /></ErrorBoundary>
      </main>
      {menu && <div className="fixed inset-0 z-40 flex" role="dialog" aria-modal="true" aria-label="Menu">
        <div className="flex h-full w-[min(20rem,85vw)] flex-col overflow-hidden border-r border-[#202a34] bg-[#0b1016] shadow-2xl"
          style={{ paddingTop: 'env(safe-area-inset-top, 0px)', paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}>
          <div className="flex items-center justify-between border-b border-[#202a34] px-3 py-2">
            <span className="text-sm font-semibold text-slate-100">Menu</span>
            <button type="button" aria-label="Close menu" onClick={() => setMenu(false)} className="h-11 w-11 text-xl text-slate-300">✕</button>
          </div>
          <nav className="grid grid-cols-2 gap-1 border-b border-[#202a34] p-2 text-sm">
            {NAV.map(([to, label]) => <Link key={to} to={to}
              className={`rounded px-3 py-3 ${location.pathname === to ? 'bg-[#1b2539] text-blue-100' : 'text-slate-300 active:bg-[#1b2539]'}`}>{label}</Link>)}
          </nav>
          <div className="flex items-center gap-2 border-b border-[#202a34] px-3 py-2 text-xs text-slate-400">
            <span>View</span>
            {[['auto', 'Automatic'], ['touch', 'Touch'], ['desktop', 'Desktop']].map(([value, label]) =>
              <button key={value} type="button" onClick={() => setMode(value === 'auto' ? null : value)}
                className={`rounded border px-2 py-1.5 ${mode === value ? 'border-blue-500 text-blue-100' : 'border-slate-700'}`}>{label}</button>)}
          </div>
          <div className="min-h-0 flex-1 overflow-hidden"><Sidebar variant="drawer" /></div>
        </div>
        <button type="button" aria-label="Close menu" className="flex-1 bg-black/60" onClick={() => setMenu(false)} />
      </div>}
    </div>
  );
}
