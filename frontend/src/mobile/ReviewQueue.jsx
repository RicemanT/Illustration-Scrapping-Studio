import React, { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';

const number = (value) => Number(value || 0).toLocaleString();

// Home screen on phones and tablets: the planner collections still to review, with "Review next".
export default function ReviewQueue() {
  const navigate = useNavigate();
  const [show, setShow] = useState('open');
  const [search, setSearch] = useState('');
  const folders = useQuery({ queryKey: ['tracker-folders'], queryFn: async () => (await api.tracker.folders()).data, refetchInterval: 30000 });
  const items = folders.data?.items || [];
  const visible = useMemo(() => items.filter((item) => (show === 'open' ? !item.completed_at : show === 'done' ? item.completed_at : true)
    && (!search || `${item.name} ${item.artist} ${item.group}`.toLowerCase().includes(search.toLowerCase()))), [items, show, search]);
  const next = items.find((item) => !item.completed_at);

  return (
    <div className="space-y-3 p-3">
      <div className="rounded-lg border border-[#202a34] bg-[#0d1219] p-3">
        <h1 className="text-base font-semibold text-slate-100">Review queue</h1>
        {folders.data && <p className="mt-1 text-xs text-slate-400">
          {number(folders.data.accepted)} of {number(folders.data.total)} collections accepted · {number(folders.data.accepted_images)} of {number(folders.data.images)} images</p>}
        {folders.data?.total > 0 && <div className="mt-2 h-1.5 overflow-hidden rounded bg-[#1b2539]">
          <div className="h-full bg-emerald-500" style={{ width: `${Math.round((folders.data.accepted / folders.data.total) * 100)}%` }} /></div>}
        <button type="button" disabled={!next} onClick={() => navigate(`/folder/${next.folder_id}`)}
          className="mt-3 w-full rounded bg-blue-700 py-3 text-sm font-medium text-white disabled:opacity-40">
          {next ? `Review next: ${next.name}` : 'Everything is accepted'}</button>
      </div>
      <div className="flex gap-1 text-xs">
        {[['open', 'To review'], ['done', 'Accepted'], ['all', 'All']].map(([key, label]) => <button key={key} type="button" onClick={() => setShow(key)}
          className={`flex-1 rounded border py-2 ${show === key ? 'border-blue-500 text-blue-100' : 'border-slate-700 text-slate-300'}`}>{label}</button>)}
      </div>
      <input type="search" aria-label="Search collections" placeholder="Search collections…" value={search} onChange={(event) => setSearch(event.target.value)}
        className="w-full rounded border border-slate-700 bg-[#090d12] px-3 py-2.5 text-sm" />
      {folders.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
      {folders.error && <p role="alert" className="text-sm text-red-400">{folders.error.message}</p>}
      {folders.data && items.length === 0 && <p className="text-sm text-slate-400">No planner collections yet. Open the menu (☰) for all collections.</p>}
      <ul className="divide-y divide-[#202a34] rounded-lg border border-[#202a34]">
        {visible.slice(0, 300).map((item) => <li key={item.folder_id}>
          <Link to={`/folder/${item.folder_id}`} className="flex items-center gap-3 px-3 py-3 active:bg-[#1b2539]">
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-slate-100">{item.name}</div>
              <div className="truncate text-xs text-slate-500">{item.group} · {item.images}{item.target ? ` / ${item.target}` : ''} images</div>
            </div>
            {item.completed_at ? <span className="text-emerald-400">✓</span> : <span className="text-slate-500">›</span>}
          </Link>
        </li>)}
      </ul>
      {visible.length > 300 && <p className="text-center text-xs text-slate-500">Showing 300 of {number(visible.length)}; search to find others.</p>}
    </div>
  );
}
