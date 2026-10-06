import React from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';

const label = (tag) => String(tag || '').replaceAll('_', ' ');
const TONE = {
  missing: 'border-rose-800/70 text-rose-100',
  lost: 'border-amber-700/70 text-amber-100',
  below: 'border-yellow-800/60 text-yellow-100',
  met: 'border-[#2a3644] text-slate-300',
};

function Chip({ row, count, onFilter, title }) {
  const tone = TONE[row.status] || TONE.met;
  return (
    <button type="button" onClick={() => onFilter(label(row.tag))} title={title}
      className={`inline-flex items-center gap-1 rounded-full border bg-[#0d141c] px-2 py-0.5 text-[11px] hover:bg-[#162130] ${tone} ${row.priority ? 'ring-1 ring-rose-500/40' : ''}`}>
      {row.priority && <span className="text-rose-300">★</span>}
      <span>{label(row.tag)}</span>
      <span className="text-slate-400">×{count}</span>
      {row.goal ? <span className={`tabular-nums ${row.now < row.goal ? 'text-amber-300' : 'text-slate-500'}`}>{row.now}/{row.goal}</span>
        : <span className="tabular-nums text-slate-500">{row.now}</span>}
    </button>
  );
}

// Characters in this planner collection, with dataset-wide counts against their goals.
export default function FolderCharacters({ folderId, imageCount, onFilter }) {
  const query = useQuery({
    queryKey: ['tracker-folder', folderId, imageCount],
    queryFn: async () => (await api.tracker.folder(folderId)).data,
    retry: false,
    // While the first dataset-wide count runs, refresh until the totals are complete.
    refetchInterval: (state) => (state.state.data?.counting ? 4000 : false),
  });
  const data = query.data;
  if (!data) return query.isError ? null : <div className="border-b border-[#202a34] px-3 py-2 text-xs text-slate-500">Counting characters…</div>;
  const short = data.characters.filter((row) => row.goal && row.now < row.goal);
  return (
    <div className="space-y-1.5 border-b border-[#202a34] px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-slate-400" title="Click a character to show its images. Counts: here ×n, then dataset-wide now/goal.">Characters</span>
        {data.characters.length === 0 && <span className="text-slate-500">None tagged in this collection.</span>}
        {data.characters.map((row) => <Chip key={row.tag} row={row} count={row.here} onFilter={onFilter}
          title={`${row.here} here · ${row.now} in the dataset${row.goal ? ` of ${row.goal} (goal)` : ''}${row.series ? ` · ${label(row.series)}` : ''}`} />)}
        {data.counting && <span className="ml-auto text-blue-200" title="Every planner collection is being counted once; dataset-wide totals are partial until then">Counting the whole dataset…</span>}
        <Link to="/tracker" className={`${data.counting ? '' : 'ml-auto '}text-slate-500 hover:text-slate-300`}>Tracker →</Link>
      </div>
      {(data.lost.length > 0 || short.length > 0) && <div className="flex flex-wrap items-center gap-1.5">
        {data.lost.length > 0 && <>
          <span className="mr-1 text-amber-300" title="The plan picked images of these characters here, but fewer are left now">Lost vs. plan</span>
          {data.lost.map((row) => <span key={row.tag} className={`rounded-full border px-2 py-0.5 text-[11px] ${row.priority ? 'border-rose-700/70 text-rose-200' : 'border-amber-800/60 text-amber-200'}`}>
            {row.priority && '★ '}{label(row.tag)} {row.here}/{row.planned_here}</span>)}
        </>}
        {short.length > 0 && <span className="ml-auto text-slate-400">{short.length} {short.length === 1 ? 'character here is' : 'characters here are'} short of their goal: sort the wildcards by <b className="text-slate-300">Fills gaps</b> to find more.</span>}
      </div>}
    </div>
  );
}
