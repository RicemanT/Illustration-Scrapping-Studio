import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';

// Every reference pair export over time, so curation progress shows without reading each export's result line.
const quiet = 'rounded border border-[#2a3644] px-2.5 py-1.5 text-xs text-slate-300 hover:bg-[#121a24] disabled:opacity-40';
const field = 'rounded border border-[#202a34] bg-[#090d12] px-2 py-1.5 text-xs text-slate-200';
const number = (value) => Number(value || 0).toLocaleString();
const day = (value) => (value ? new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '');
const scopeOf = (item) => `${item.accepted_only ? 'Accepted' : 'All'} folders · ${item.groups ? item.groups.join(', ') : 'every group'}`;

const METRICS = [
  { key: 'artists', label: 'Artists', get: (s) => s.coverage?.artists, goal: 1952, goalNote: 'Anima-Ref v2 styles' },
  { key: 'images', label: 'Images', get: (s) => s.images },
  { key: 'two', label: 'Characters by 2+ artists', get: (s) => s.coverage?.characters_2_plus_artists, goal: 2300, goalNote: 'Anima-Ref v2 characters' },
  { key: 'five', label: 'Characters by 5+ artists', get: (s) => s.coverage?.characters_5_plus_artists },
  { key: 'identity', label: 'Identity pairs', get: (s) => (s.by_kind?.identity || 0) + (s.by_kind?.identity_style || 0) },
  { key: 'style', label: 'Style pairs', get: (s) => s.by_kind?.style },
];

function Sparkline({ points }) {
  const width = 160, height = 36, pad = 4;
  const values = points.map((p) => p.value);
  const min = Math.min(...values), max = Math.max(...values);
  const x = (i) => (points.length === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (points.length - 1));
  const y = (v) => (max === min ? height / 2 : height - pad - ((v - min) * (height - 2 * pad)) / (max - min));
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-9 w-full" preserveAspectRatio="none" role="img" aria-label="Trend across exports">
      {points.length > 1 && <polyline fill="none" stroke="rgb(52 211 153 / 0.85)" strokeWidth="2" vectorEffect="non-scaling-stroke"
        strokeLinejoin="round" strokeLinecap="round" points={points.map((p, i) => `${x(i)},${y(p.value)}`).join(' ')} />}
      {points.map((p, i) => (
        <g key={p.name}>
          <circle cx={x(i)} cy={y(p.value)} r="8" fill="transparent"><title>{`${p.name} (${day(p.date)}): ${number(p.value)}`}</title></circle>
          {i === points.length - 1 && <circle cx={x(i)} cy={y(p.value)} r="3" fill="rgb(52 211 153)" stroke="#0c1219" strokeWidth="2" pointerEvents="none" />}
        </g>
      ))}
    </svg>
  );
}

function Tile({ metric, series }) {
  const points = series.map((s) => ({ name: s.name, date: s.created_at, value: metric.get(s) || 0 }));
  const last = points[points.length - 1]?.value || 0;
  const delta = points.length > 1 ? last - points[points.length - 2].value : null;
  return (
    <div className="rounded border border-[#1d2731] p-3">
      <div className="text-xs text-slate-400">{metric.label}</div>
      <div className="mt-0.5 flex items-baseline gap-2">
        <span className="text-xl font-semibold tabular-nums text-slate-100">{number(last)}</span>
        {delta != null && <span className={`text-xs tabular-nums ${delta > 0 ? 'text-emerald-300' : delta < 0 ? 'text-rose-300' : 'text-slate-500'}`}>
          {delta > 0 ? '+' : ''}{number(delta)} since the last export</span>}
      </div>
      {metric.goal && <div className="mt-1.5 space-y-1">
        <div className="h-1.5 overflow-hidden rounded bg-[#1a232e]"><div className="h-full bg-blue-500/70" style={{ width: `${Math.min(100, (100 * last) / metric.goal)}%` }} /></div>
        <div className="text-[11px] text-slate-500">{last >= metric.goal ? 'Reached' : `${Math.round((100 * last) / metric.goal)}% of`} {number(metric.goal)} ({metric.goalNote})</div>
      </div>}
      {points.length > 0 && <div className="mt-1.5"><Sparkline points={points} /></div>}
    </div>
  );
}

export default function ReferencePairsHistory() {
  const history = useQuery({ queryKey: ['reference-pairs-history'], queryFn: async () => (await api.settings.referencePairsHistory()).data });
  const items = history.data?.items || [];
  const scopes = useMemo(() => [...new Set(items.map(scopeOf))], [items]);
  const [picked, setPicked] = useState(null);
  const [copied, setCopied] = useState(false);
  const scope = picked && scopes.includes(picked) ? picked : (items.length ? scopeOf(items[items.length - 1]) : null);
  const series = items.filter((item) => scopeOf(item) === scope);

  const copy = async () => {
    const header = ['name', 'date', 'scope', ...METRICS.map((m) => m.label), 'held out'];
    const rows = items.map((s) => [s.name, (s.created_at || '').slice(0, 10), scopeOf(s), ...METRICS.map((m) => m.get(s) || 0), s.held_out]);
    try { await navigator.clipboard.writeText([header, ...rows].map((row) => row.join('\t')).join('\n')); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch { /* clipboard unavailable */ }
  };

  return (
    <section className="rounded border border-[#202a34] bg-[#0c1219] p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-semibold text-slate-100">Reference pair exports</h2>
          <p className="mt-1 text-xs text-slate-400">Every export from Settings → Reference pairs. Export now and then under the date's name; the trend lines follow the exports with the same scope as the latest one.</p>
        </div>
        <div className="flex items-center gap-2">
          {scopes.length > 1 && <select className={`${field} max-w-64`} value={scope || ''} onChange={(event) => setPicked(event.target.value)}>
            {scopes.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>}
          <button type="button" className={quiet} disabled={!items.length} onClick={copy}>{copied ? 'Copied' : 'Copy table'}</button>
        </div>
      </div>
      {history.isLoading && <p className="text-xs text-slate-400">Loading…</p>}
      {!history.isLoading && !items.length && <p className="text-xs text-slate-400">No pair exports yet.</p>}
      {series.length > 0 && <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        {METRICS.map((metric) => <Tile key={metric.key} metric={metric} series={series} />)}
      </div>}
      {items.length > 0 && <div className="max-h-64 overflow-auto rounded border border-[#1d2731]">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-[#0e151d] text-left text-slate-400">
            <tr>
              <th className="px-2 py-2">Export</th><th className="px-2 py-2">Date</th><th className="px-2 py-2">Scope</th>
              {METRICS.map((m) => <th key={m.key} className="px-2 py-2 text-right">{m.label}</th>)}
              <th className="px-2 py-2 text-right">Held out</th>
            </tr>
          </thead>
          <tbody>
            {[...items].reverse().map((s) => (
              <tr key={s.name} className="border-t border-[#151d26]">
                <td className="px-2 py-1.5 font-mono text-slate-200">{s.name}</td>
                <td className="px-2 py-1.5 text-slate-400">{day(s.created_at)}</td>
                <td className="max-w-56 truncate px-2 py-1.5 text-slate-500" title={scopeOf(s)}>{scopeOf(s)}</td>
                {METRICS.map((m) => <td key={m.key} className="px-2 py-1.5 text-right tabular-nums text-slate-300">{number(m.get(s))}</td>)}
                <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{s.held_out}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
    </section>
  );
}
