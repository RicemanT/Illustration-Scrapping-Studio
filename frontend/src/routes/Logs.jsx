import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';

export default function Logs() {
  const [params] = useSearchParams();
  const [filters, setFilters] = useState({ level: 'ACTIVITY', search: '', request_id: params.get('request_id') || '', job_id: params.get('job_id') || '' });
  const [before, setBefore] = useState('');
  const [live, setLive] = useState(true);
  const query = useQuery({ queryKey: ['diagnostics', filters, before], queryFn: async () => (await api.diagnostics.list(Object.fromEntries(Object.entries({ ...filters, before }).filter(([, value]) => value !== '')))).data, refetchInterval: live && !before ? 4000 : false });
  const change = (key, value) => { setFilters(old => ({ ...old, [key]: value })); setBefore(''); };
  const exportPage = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(query.data, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = 'artist-diagnostics.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <section className="space-y-4 text-sm">
    <h1 className="text-xl font-semibold text-slate-100">Logs &amp; Diagnostics</h1>
    <p className="text-slate-400">Requests, jobs and failure evidence. Confirmed observations are separated from possible causes. Old events expire as the bounded history rotates.</p>
    <div className="flex flex-wrap gap-2">
      <select aria-label="Log severity" value={filters.level} onChange={e => change('level', e.target.value)} className="bg-slate-900 rounded p-2"><option value="ACTIVITY">Activity and problems</option><option value="">All (include reads)</option>{['ERROR', 'WARNING', 'INFO', 'DEBUG'].map(v => <option key={v}>{v}</option>)}</select>
      {['search', 'request_id', 'job_id', 'folder_id', 'provider'].map(key => <input key={key} aria-label={key} placeholder={key.replaceAll('_', ' ')} value={filters[key] || ''} onChange={e => change(key, e.target.value)} className="bg-slate-900 border border-slate-700 rounded p-2" />)}
      <label className="p-2"><input type="checkbox" checked={live} onChange={e => setLive(e.target.checked)} /> Live</label>
      <button onClick={() => { setBefore(''); query.refetch(); }}>Newest</button>
      <button onClick={exportPage} disabled={!query.data}>Export this page</button>
    </div>
    {query.isError && <p role="alert" className="text-red-300">Logs could not be loaded: {query.error.message}</p>}
    {query.data?.storage && <p className={query.data.storage.writable === false ? 'text-red-300' : 'text-slate-500'}>Storage: {query.data.storage.directory}. {query.data.storage.last_error || query.data.storage.retention}</p>}
    {query.isLoading && <p>Loading history...</p>}
    {query.data?.items.length === 0 && <p>No retained events match these filters.</p>}
    <div className="space-y-2">{query.data?.items.map(item => <details key={item.event_id} className="surface-panel rounded border border-slate-700 p-3">
      <summary className="cursor-pointer break-words"><span className={item.level === 'ERROR' ? 'text-red-300' : item.level === 'WARNING' ? 'text-amber-300' : 'text-blue-200'}>{item.level}</span> <span className="text-slate-500">{new Date(item.at).toLocaleString()}</span> {item.message}</summary>
      {item.diagnostic && <div className="my-3 space-y-2"><p className="font-semibold">{item.diagnostic.summary} ({item.diagnostic.certainty})</p><p>Evidence: {item.diagnostic.evidence}</p><p>Possible causes: {item.diagnostic.likely_causes.join(' ')}</p><ul className="list-disc pl-5">{item.diagnostic.next_steps.map(step => <li key={step}>{step}</li>)}</ul></div>}
      <div className="flex gap-4 my-2">{item.request_id && <button onClick={() => change('request_id', item.request_id)}>Show request</button>}{item.job_id && <button onClick={() => change('job_id', item.job_id)}>Show job</button>}</div>
      <pre className="overflow-auto whitespace-pre-wrap break-all text-xs text-slate-400">{JSON.stringify(item, null, 2)}</pre>
    </details>)}</div>
    {query.data?.next_cursor && <button onClick={() => setBefore(query.data.next_cursor)}>Older events</button>}
  </section>;
}
