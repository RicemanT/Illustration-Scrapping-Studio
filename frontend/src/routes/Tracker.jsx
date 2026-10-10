import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';

const field = 'rounded border border-[#202a34] bg-[#090d12] px-2 py-1.5 text-xs text-slate-200';
const quiet = 'rounded border border-[#2a3644] px-2.5 py-1.5 text-xs text-slate-300 hover:bg-[#121a24] disabled:opacity-40';
const primary = 'rounded bg-[#344a73] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#405b88] disabled:opacity-40';
const number = (value) => Number(value || 0).toLocaleString();
const label = (tag) => String(tag || '').replaceAll('_', ' ');
const formatDate = (value) => (value ? new Date(value).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '');
const STALE_MS = 30 * 60 * 1000;
const STATUS = {
  missing: { text: 'missing', cls: 'bg-rose-950/70 text-rose-200 ring-rose-800/70' },
  lost: { text: 'lost in curation', cls: 'bg-amber-950/70 text-amber-200 ring-amber-800/70' },
  below: { text: 'below goal', cls: 'bg-yellow-950/60 text-yellow-200 ring-yellow-800/60' },
  met: { text: 'ok', cls: 'bg-emerald-950/60 text-emerald-200 ring-emerald-800/60' },
};
const FAMILIES = [['danbooru', 'Danbooru + Gelbooru'], ['e621', 'e621']];

function stored(key, fallback) {
  try { const value = localStorage.getItem(`tracker:${key}`); return value == null ? fallback : JSON.parse(value); } catch { return fallback; }
}
function usePersisted(key, fallback) {
  const [value, setValue] = useState(() => stored(key, fallback));
  useEffect(() => { try { localStorage.setItem(`tracker:${key}`, JSON.stringify(value)); } catch { /* storage unavailable */ } }, [key, value]);
  return [value, setValue];
}

function StatusChip({ status }) {
  const item = STATUS[status] || STATUS.met;
  return <span className={`whitespace-nowrap rounded px-1.5 py-0.5 text-[10px] ring-1 ${item.cls}`}>{item.text}</span>;
}

function Bar({ value, total, tone = 'bg-emerald-500/80' }) {
  const share = total ? Math.min(100, Math.round((100 * value) / total)) : 0;
  return <div className="h-1.5 w-full overflow-hidden rounded bg-[#1a232e]"><div className={`h-full ${tone}`} style={{ width: `${share}%` }} /></div>;
}

function FoldersCard() {
  const folders = useQuery({ queryKey: ['tracker-folders'], queryFn: async () => (await api.tracker.folders()).data, refetchInterval: 30000 });
  const [showAccepted, setShowAccepted] = usePersisted('showAccepted', false);
  const [filter, setFilter] = useState('');
  const data = folders.data;
  const groups = useMemo(() => {
    const result = new Map();
    for (const item of data?.items || []) {
      const key = item.group || 'Ungrouped';
      const entry = result.get(key) || { name: key, total: 0, accepted: 0 };
      entry.total += 1;
      entry.accepted += item.completed_at ? 1 : 0;
      result.set(key, entry);
    }
    return [...result.values()];
  }, [data]);
  const visible = (data?.items || []).filter((item) => (showAccepted || !item.completed_at)
    && (!filter || `${item.name} ${item.group || ''}`.toLowerCase().includes(filter.toLowerCase())));
  return (
    <section className="rounded border border-[#202a34] bg-[#0c1219] p-4 space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold text-slate-100">Collections</h2>
        {data && <span className="text-xs text-slate-400">{number(data.accepted)} of {number(data.total)} accepted · {number(data.accepted_images)} of {number(data.images)} images in accepted collections</span>}
      </div>
      {data && <Bar value={data.accepted} total={data.total} />}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {groups.map((group) => (
          <div key={group.name} className="rounded border border-[#1d2731] p-2 text-xs">
            <div className="mb-1 flex justify-between gap-2"><span className="truncate text-slate-300">{group.name}</span><span className="text-slate-400">{group.accepted} / {group.total}</span></div>
            <Bar value={group.accepted} total={group.total} tone="bg-blue-500/70" />
          </div>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-slate-300">{showAccepted ? 'All collections' : 'Still to review'}</span>
        <input className={`${field} w-48`} placeholder="Filter by name or group" value={filter} onChange={(event) => setFilter(event.target.value)} />
        <label className="flex items-center gap-1 text-xs text-slate-400"><input type="checkbox" checked={showAccepted} onChange={(event) => setShowAccepted(event.target.checked)} /> Show accepted</label>
      </div>
      <div className="max-h-56 overflow-y-auto rounded border border-[#1d2731]">
        {visible.length === 0 && <p className="p-3 text-xs text-slate-400">{data ? 'Nothing to review here.' : 'Loading…'}</p>}
        {visible.map((item) => (
          <Link key={item.folder_id} to={`/folder/${item.folder_id}`} className="flex items-center gap-3 border-b border-[#151d26] px-3 py-1.5 text-xs last:border-0 hover:bg-[#121a24]">
            <span className={`w-4 ${item.completed_at ? 'text-emerald-400' : 'text-slate-600'}`}>{item.completed_at ? '✓' : '○'}</span>
            <span className="flex-1 truncate text-slate-200">{item.name}</span>
            <span className="hidden truncate text-slate-500 sm:block">{item.group}</span>
            <span className={`tabular-nums ${item.target && item.images < item.target ? 'text-amber-300' : 'text-slate-400'}`}>{item.images}{item.target ? ` / ${item.target}` : ''}</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

function SnapshotsCard({ compare, setCompare }) {
  const queryClient = useQueryClient();
  const snapshots = useQuery({ queryKey: ['tracker-snapshots'], queryFn: async () => (await api.tracker.snapshots()).data });
  const [name, setName] = useState('');
  const create = useMutation({
    meta: { successMessage: 'Snapshot saved' },
    mutationFn: () => api.tracker.createSnapshot(name.trim()),
    onSuccess: ({ data }) => { setName(''); setCompare(data.id); queryClient.invalidateQueries({ queryKey: ['tracker-snapshots'] }); },
  });
  const remove = useMutation({
    mutationFn: (id) => api.tracker.deleteSnapshot(id),
    onSuccess: (_, id) => { if (compare === id) setCompare(null); queryClient.invalidateQueries({ queryKey: ['tracker-snapshots'] }); },
  });
  const items = snapshots.data?.items || [];
  // Release lists: characters / artists of chosen groups (e.g. the three pilot groups), for a model card.
  const folders = useQuery({ queryKey: ['tracker-folders'], queryFn: async () => (await api.tracker.folders()).data });
  const groups = useMemo(() => {
    const byId = new Map();
    for (const item of folders.data?.items || []) {
      if (item.group_id == null) continue;
      const entry = byId.get(item.group_id) || { id: item.group_id, name: item.group || 'Ungrouped', folders: 0, images: 0 };
      entry.folders += 1;
      entry.images += item.images;
      byId.set(item.group_id, entry);
    }
    return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [folders.data]);
  const [releaseGroups, setReleaseGroups] = usePersisted('releaseGroups', []);
  const picked = releaseGroups.filter((id) => groups.some((group) => group.id === id));
  const releaseUrl = (kind) => backendAssetUrl(`/api/tracker/release.csv?kind=${kind}${picked.map((id) => `&group_ids=${id}`).join('')}`);
  const pickedGroups = groups.filter((group) => picked.includes(group.id));
  return (
    <section className="rounded border border-[#202a34] bg-[#0c1219] p-4 space-y-3">
      <div>
        <h2 className="font-semibold text-slate-100">Release snapshots</h2>
        <p className="mt-1 text-xs text-slate-400">Save the counts when you train a release (for example v0.4). Compare the tables against a snapshot to see what each batch added, and download either as CSV.</p>
      </div>
      <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); if (name.trim()) create.mutate(); }}>
        <input className={`${field} w-40`} placeholder="v0.4" value={name} maxLength={80} onChange={(event) => setName(event.target.value)} />
        <button className={primary} disabled={!name.trim() || create.isPending}>Save snapshot</button>
      </form>
      {create.isError && <p className="text-xs text-red-300">{create.error.response?.data?.detail || create.error.message}</p>}
      <div className="space-y-1.5">
        {items.length === 0 && <p className="text-xs text-slate-500">No snapshots yet.</p>}
        {items.map((item) => (
          <div key={item.id} className={`flex flex-wrap items-center gap-2 rounded border px-2 py-1.5 text-xs ${compare === item.id ? 'border-blue-700/70 bg-blue-950/20' : 'border-[#1d2731]'}`}>
            <span className="font-semibold text-slate-200">{item.name}</span>
            <span className="text-slate-500">{formatDate(item.created_at)}</span>
            <span className="text-slate-400">{number(item.summary.accepted)}/{number(item.summary.folders)} accepted · {number(item.summary.images)} images</span>
            <span className="ml-auto flex gap-1.5">
              <button type="button" className={quiet} onClick={() => setCompare(compare === item.id ? null : item.id)}>{compare === item.id ? 'Comparing' : 'Compare'}</button>
              <a className={quiet} href={backendAssetUrl(`/api/tracker/snapshots/${item.id}/export.csv`)}>CSV</a>
              <button type="button" className={quiet} onClick={() => { if (window.confirm(`Delete snapshot ${item.name}?`)) remove.mutate(item.id); }}>Delete</button>
            </span>
          </div>
        ))}
      </div>
      <div className="space-y-2 border-t border-[#1d2731] pt-3">
        <h3 className="text-sm font-semibold text-slate-200">Release lists</h3>
        <p className="text-xs text-slate-400">The characters and artists of the groups you tick (none ticked = every planner group), as CSV for a release's model card. Characters count the collections' current images.</p>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {groups.map((group) => <label key={group.id} className="flex items-center gap-1.5 text-xs text-slate-300">
            <input type="checkbox" checked={picked.includes(group.id)}
              onChange={(event) => setReleaseGroups(event.target.checked ? [...picked, group.id] : picked.filter((id) => id !== group.id))} />
            {group.name} <span className="text-slate-500">({number(group.folders)})</span></label>)}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a className={quiet} href={releaseUrl('characters')}>Characters CSV</a>
          <a className={quiet} href={releaseUrl('artists')}>Artists CSV</a>
          <span className="text-xs text-slate-500">{pickedGroups.length
            ? `${number(pickedGroups.reduce((sum, group) => sum + group.folders, 0))} artists · ${number(pickedGroups.reduce((sum, group) => sum + group.images, 0))} images`
            : 'all planner groups'}</span>
        </div>
      </div>
    </section>
  );
}

function GoalEditor({ kind, row, onSaved }) {
  const [value, setValue] = useState(row.custom_goal ?? '');
  const save = useMutation({
    mutationFn: ({ tags, series, goal }) => api.tracker.setGoal({ kind, family: row.family, tags, series, goal }),
    onSuccess: onSaved,
  });
  const goal = value === '' ? null : Number(value);
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-slate-400">Your goal</span>
      <input type="number" min="0" className={`${field} w-20`} value={value} placeholder={row.floor ?? '—'} onChange={(event) => setValue(event.target.value)} />
      <button type="button" className={primary} disabled={save.isPending} onClick={() => save.mutate({ tags: [row.tag], goal })}>{goal == null ? (row.floor != null ? 'Use the floor' : 'Clear') : 'Save goal'}</button>
      {kind === 'character' && row.series && <button type="button" className={quiet} disabled={save.isPending}
        onClick={() => save.mutate({ tags: null, series: row.series, goal })}>Apply to every {label(row.series)} character</button>}
      {save.isError && <span className="text-red-300">{save.error.response?.data?.detail || save.error.message}</span>}
    </div>
  );
}

function CharacterFolders({ row }) {
  const folders = useQuery({ queryKey: ['tracker-character-folders', row.family, row.tag], queryFn: async () => (await api.tracker.characterFolders(row.family, row.tag)).data });
  const items = folders.data?.items || [];
  if (folders.isLoading) return <p className="text-xs text-slate-400">Loading collections…</p>;
  if (!items.length) return <p className="text-xs text-slate-400">No collection has or could add this character yet.</p>;
  const has = items.filter((item) => item.now > 0);
  const could = items.filter((item) => item.now === 0 && item.spare > 0);
  const lost = items.filter((item) => item.planned > item.now);
  const link = (item, text) => <Link key={item.folder_id} to={`/folder/${item.folder_id}`} className="rounded bg-[#151d26] px-2 py-0.5 text-slate-200 hover:bg-[#1f2a37]">{item.name} <span className="text-slate-400">{text}</span>{item.accepted && <span className="ml-1 text-emerald-400">✓</span>}</Link>;
  return (
    <div className="space-y-2 text-xs">
      {has.length > 0 && <div className="flex flex-wrap items-center gap-1.5"><span className="w-28 text-slate-400">In collections</span>{has.map((item) => link(item, `×${item.now}`))}</div>}
      {lost.length > 0 && <div className="flex flex-wrap items-center gap-1.5"><span className="w-28 text-amber-300">Lost in curation</span>{lost.map((item) => link(item, `${item.now}/${item.planned} planned`))}</div>}
      {could.length > 0 && <div className="flex flex-wrap items-center gap-1.5"><span className="w-28 text-slate-400">In wildcards</span>{could.map((item) => link(item, `${item.spare} posts`))}</div>}
    </div>
  );
}

function TagTable({ kind, compare }) {
  const queryClient = useQueryClient();
  const [family, setFamily] = usePersisted(`${kind}:family`, 'danbooru');
  const [search, setSearch] = useState('');
  const [status, setStatus] = usePersisted(`${kind}:status`, '');
  const [series, setSeries] = useState('');
  const [priorityOnly, setPriorityOnly] = usePersisted(`${kind}:priority`, false);
  const [targetsOnly, setTargetsOnly] = usePersisted(`${kind}:targets`, kind === 'character');
  const [boostOnly, setBoostOnly] = usePersisted(`${kind}:boost`, false);
  const [presentOnly, setPresentOnly] = usePersisted(`${kind}:present`, false);
  const [sort, setSort] = usePersisted(`${kind}:sort`, kind === 'character' ? 'gap' : 'now');
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState(null);
  const [debounced, setDebounced] = useState('');
  useEffect(() => { const timer = setTimeout(() => setDebounced(search), 250); return () => clearTimeout(timer); }, [search]);
  useEffect(() => { setOffset(0); setOpen(null); }, [family, debounced, status, series, priorityOnly, targetsOnly, boostOnly, presentOnly, sort, compare]);
  const params = {
    family, search: debounced, status: status || undefined, sort, offset, limit: 100, snapshot_id: compare || undefined,
    present_only: presentOnly || undefined,
    ...(kind === 'character' ? { series: series || undefined, priority_only: priorityOnly || undefined, targets_only: targetsOnly || undefined }
      : { boost_only: boostOnly || undefined }),
  };
  const table = useQuery({
    queryKey: ['tracker-table', kind, params],
    queryFn: async () => (await (kind === 'character' ? api.tracker.characters(params) : api.tracker.general(params))).data,
    placeholderData: (previous) => previous,
  });
  const data = table.data;
  const summary = data?.summary;
  const items = data?.items || [];
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['tracker-table'] });
  const csv = backendAssetUrl(`/api/tracker/export.csv?kind=${kind}${compare ? `&snapshot_id=${compare}` : ''}`);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select className={field} value={family} onChange={(event) => setFamily(event.target.value)}>{FAMILIES.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select>
        <input className={`${field} w-48`} placeholder={kind === 'character' ? 'Search characters' : 'Search tags'} value={search} onChange={(event) => setSearch(event.target.value)} />
        <select className={field} value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">Any status</option><option value="missing">Missing</option><option value="lost">Lost in curation</option><option value="below">Below goal</option><option value="met">OK</option>
        </select>
        {kind === 'character' && <select className={`${field} max-w-48`} value={series} onChange={(event) => setSeries(event.target.value)}>
          <option value="">All series</option>{(data?.series || []).map((name) => <option key={name} value={name}>{label(name)}</option>)}
        </select>}
        {kind === 'character' && <label className="flex items-center gap-1 text-xs text-slate-400"><input type="checkbox" checked={priorityOnly} onChange={(event) => setPriorityOnly(event.target.checked)} /> Priority only</label>}
        {kind === 'character' && <label className="flex items-center gap-1 text-xs text-slate-400" title="Characters from the planner's target list"><input type="checkbox" checked={targetsOnly} onChange={(event) => setTargetsOnly(event.target.checked)} /> Targets only</label>}
        {kind === 'general' && <label className="flex items-center gap-1 text-xs text-slate-400" title="The plan's boost tags"><input type="checkbox" checked={boostOnly} onChange={(event) => setBoostOnly(event.target.checked)} /> Boost tags only</label>}
        <label className="flex items-center gap-1 text-xs text-slate-400"><input type="checkbox" checked={presentOnly} onChange={(event) => setPresentOnly(event.target.checked)} /> In the dataset</label>
        <select className={field} value={sort} onChange={(event) => setSort(event.target.value)}>
          {kind === 'character' && <option value="gap">Biggest gap first</option>}
          <option value="now">Most images</option><option value="lost">Most lost in curation</option><option value="spare">Most spare in wildcards</option>
          {kind === 'character' && <option value="rank">Target rank</option>}
          {kind === 'general' && <option value="gap">Biggest gap first</option>}
          <option value="name">Name</option>{compare && <option value="delta">Change since snapshot</option>}
        </select>
        <a className={`${quiet} ml-auto`} href={csv}>Download CSV</a>
      </div>
      {summary && <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
        <span><b className="text-slate-200">{number(summary.present)}</b> in the dataset{kind === 'character' ? ` · ${number(summary.targets)} targets` : ''}</span>
        {['missing', 'lost', 'below', 'met'].map((name) => summary.statuses?.[name] ? <button key={name} type="button" onClick={() => setStatus(status === name ? '' : name)} className="hover:text-slate-200"><StatusChip status={name} /> {number(summary.statuses[name])}</button> : null)}
        {kind === 'character' && summary.priority_below > 0 && <button type="button" onClick={() => { setPriorityOnly(true); setStatus(''); }} className="text-rose-300 hover:text-rose-200">★ {number(summary.priority_below)} priority characters not OK</button>}
        {summary.lost_images > 0 && <span className="text-amber-300">{number(summary.lost_images)} planned images lost in curation</span>}
      </div>}
      <div className="overflow-x-auto rounded border border-[#1d2731]">
        <table className="w-full text-xs">
          <thead className="bg-[#0e151d] text-left text-slate-400">
            <tr>
              <th className="px-2 py-2">{kind === 'character' ? 'Character' : 'Tag'}</th>
              {kind === 'character' && <th className="px-2 py-2">Series</th>}
              <th className="px-2 py-2 text-right" title="Images the plans picked">Planned</th>
              <th className="px-2 py-2 text-right" title="Images in the collections now">Now</th>
              <th className="px-2 py-2 text-right" title="Images in accepted collections">Accepted</th>
              <th className="px-2 py-2 text-right" title="Your goal, or the planner's character floor">Goal</th>
              <th className="px-2 py-2 text-right">Gap</th>
              <th className="px-2 py-2 text-right" title="Usable posts not yet in collections, in collections still under review (and how many artists)">Spare</th>
              <th className="px-2 py-2 text-right" title="Collections with at least one image">Collections</th>
              {compare && <th className="px-2 py-2 text-right">Δ</th>}
              <th className="px-2 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => {
              const key = `${row.family}:${row.tag}`;
              const expanded = open === key;
              return (
                <React.Fragment key={key}>
                  <tr onClick={() => setOpen(expanded ? null : key)} className={`cursor-pointer border-t border-[#151d26] hover:bg-[#111922] ${expanded ? 'bg-[#111922]' : ''}`}>
                    <td className="px-2 py-1.5 text-slate-200">{row.priority && <span className="mr-1 text-rose-300" title="Priority series">★</span>}{label(row.tag)}{row.boost && <span className="ml-1 rounded bg-blue-950 px-1 text-[10px] text-blue-200">boost</span>}</td>
                    {kind === 'character' && <td className="max-w-40 truncate px-2 py-1.5 text-slate-400">{label(row.series)}</td>}
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{number(row.planned)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-100">{number(row.now)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-emerald-300/90">{number(row.accepted)}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-300" title={row.custom_goal != null ? 'Your goal' : row.floor != null ? "The planner's character floor" : ''}>{row.goal ?? '—'}{row.custom_goal != null && <span className="text-blue-300">*</span>}</td>
                    <td className={`px-2 py-1.5 text-right tabular-nums ${row.gap ? 'text-amber-300' : 'text-slate-600'}`}>{row.gap || '—'}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{number(row.spare)}{row.spare_artists ? <span className="text-slate-600"> ({row.spare_artists})</span> : null}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">{number(row.folders)}</td>
                    {compare && <td className={`px-2 py-1.5 text-right tabular-nums ${row.delta > 0 ? 'text-emerald-300' : row.delta < 0 ? 'text-rose-300' : 'text-slate-600'}`}>{row.delta > 0 ? `+${row.delta}` : row.delta || '—'}</td>}
                    <td className="px-2 py-1.5">{row.status ? <StatusChip status={row.status} /> : null}</td>
                  </tr>
                  {expanded && <tr className="bg-[#0d141b]"><td colSpan={compare ? 11 : 10} className="space-y-3 px-3 py-3">
                    {kind === 'character' ? <CharacterFolders row={row} /> : <p className="text-xs text-slate-400">Open a collection and use the gallery's tag filter (“{label(row.tag)}”) to see its images.</p>}
                    <GoalEditor kind={kind} row={row} onSaved={refresh} />
                  </td></tr>}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
        {items.length === 0 && <p className="p-4 text-xs text-slate-400">{table.isLoading ? 'Loading…' : 'Nothing matches these filters.'}</p>}
      </div>
      <div className="flex items-center gap-3 text-xs text-slate-400">
        <button type="button" className={quiet} disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</button>
        <span>{data ? `${number(offset + (items.length ? 1 : 0))}–${number(offset + items.length)} of ${number(data.total)}` : ''}</span>
        <button type="button" className={quiet} disabled={data?.next_offset == null} onClick={() => setOffset(data.next_offset)}>Next</button>
      </div>
    </div>
  );
}

export default function Tracker() {
  const queryClient = useQueryClient();
  const [tab, setTab] = usePersisted('tab', 'character');
  const [compare, setCompare] = usePersisted('compare', null);
  const status = useQuery({
    queryKey: ['tracker-status'],
    queryFn: async () => (await api.tracker.status()).data,
    refetchInterval: (query) => (query.state.data?.job?.status === 'running' ? 1500 : false),
  });
  const refresh = useMutation({
    mutationFn: api.tracker.refresh,
    onSuccess: ({ data }) => { queryClient.setQueryData(['tracker-status'], (old) => ({ ...(old || {}), job: data })); status.refetch(); },
  });
  const job = status.data?.job;
  const running = job?.status === 'running';
  const meta = status.data?.meta;
  const autoStarted = useRef(false);
  useEffect(() => {
    if (!status.data || running || autoStarted.current) return;
    const age = meta?.refreshed_at ? Date.now() - new Date(meta.refreshed_at).getTime() : Infinity;
    if (age > STALE_MS) { autoStarted.current = true; refresh.mutate(); }
  }, [status.data, running, meta?.refreshed_at]);
  // Reload the tables whenever a recount finished (however quickly).
  const countedAt = useRef(undefined);
  useEffect(() => {
    const at = meta?.refreshed_at;
    if (countedAt.current !== undefined && at && at !== countedAt.current) {
      for (const key of [['tracker-table'], ['tracker-folders'], ['tracker-character-folders']]) queryClient.invalidateQueries({ queryKey: key });
    }
    if (status.data) countedAt.current = at || null;
  }, [meta?.refreshed_at, status.data, queryClient]);

  return (
    <div className="mx-auto max-w-7xl space-y-4 text-slate-300">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Tracker</h1>
          <p className="text-sm text-slate-400">Characters and general tags across every planner collection: the pilot and each batch after it. Character counts update as you curate; general tags are recounted in the background.</p>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-400">
          {running ? <span role="status" className="text-blue-200">Recounting… {number(job.done)} of {number(job.total)} collections</span>
            : meta ? <span>General tags counted {formatDate(meta.refreshed_at)}</span> : <span>Not counted yet</span>}
          {job?.status === 'failed' && <span className="text-red-300">Recount failed: {job.error}</span>}
          <button type="button" className={quiet} disabled={running || refresh.isPending} onClick={() => refresh.mutate()}>Recount now</button>
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
        <FoldersCard />
        <SnapshotsCard compare={compare} setCompare={setCompare} />
      </div>
      <section className="rounded border border-[#202a34] bg-[#0c1219] p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2 border-b border-[#202a34] pb-2">
          {[['character', 'Characters'], ['general', 'General tags']].map(([value, text]) => (
            <button key={value} type="button" onClick={() => setTab(value)} className={`rounded px-3 py-1 text-xs ${tab === value ? 'bg-[#273451] text-blue-200' : 'text-slate-400 hover:text-slate-200'}`}>{text}</button>
          ))}
          {compare && <span className="ml-auto text-xs text-blue-200">Comparing with a snapshot (Δ column) <button type="button" className="ml-1 text-slate-400 hover:text-slate-200" onClick={() => setCompare(null)}>×</button></span>}
        </div>
        <TagTable key={tab} kind={tab} compare={compare} />
      </section>
    </div>
  );
}
