import { createPortal } from 'react-dom';
import React, { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import { refreshFolderViews } from '../api/folderCache';
import { justifiedRows } from './justifiedLayout';
import useDragSelect from './useDragSelect';

const PAGE = 100;
const keyOf = (item) => `${item.site}:${item.remote_id}`;
const previewUrl = (artistId, item, size) => backendAssetUrl(`/api/planner/previews/${artistId}/${item.site}/${item.remote_id}?size=${size}`);
const stored = (key, fallback) => { try { const value = localStorage.getItem(key); return value == null ? fallback : JSON.parse(value); } catch { return fallback; } };
const VIDEO = new Set(['mp4', 'webm', 'zip', 'gif', 'swf']);

// Harvested posts of this folder's planner artist that are not in the folder.
// Accepting them locks them in the planner and imports them into the folder.
function PlannerCandidates({ folderId, context, tileHeight, hotkeysActive, onHover }) {
  const queryClient = useQueryClient();
  const [sort, setSort] = useState(() => stored('artist.candidates.sort', 'popular'));
  const [includeFiltered, setIncludeFiltered] = useState(() => stored('artist.candidates.filtered', false));
  const [includeBanned, setIncludeBanned] = useState(() => stored('artist.candidates.banned', false));
  const [open, setOpen] = useState(() => stored('artist.candidates.open', true));
  useEffect(() => { try { localStorage.setItem('artist.candidates.sort', JSON.stringify(sort)); localStorage.setItem('artist.candidates.filtered', JSON.stringify(includeFiltered)); localStorage.setItem('artist.candidates.banned', JSON.stringify(includeBanned)); localStorage.setItem('artist.candidates.open', JSON.stringify(open)); } catch {} }, [sort, includeFiltered, includeBanned, open]);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [viewing, setViewing] = useState(null);
  const [jobIds, setJobIds] = useState([]);
  useEffect(() => { setOffset(0); }, [sort, includeFiltered, includeBanned]);
  useEffect(() => () => onHover(false), []);

  const completed = Boolean(context.completed_at);
  const [eraOnly, setEraOnly] = useState(true);
  const minYear = eraOnly ? context.era_from : null;
  useEffect(() => { setOffset(0); }, [minYear]);
  const { data, isFetching, error } = useQuery({
    queryKey: ['planner-candidates', folderId, sort, includeFiltered, includeBanned, offset, minYear],
    queryFn: async () => (await api.planner.candidates(folderId, { sort, include_filtered: includeFiltered, include_banned: includeBanned, offset, limit: PAGE, min_year: minYear || undefined })).data,
    enabled: open,
    placeholderData: (previous) => previous,
  });
  const items = data?.items || [];

  const { data: jobs } = useQuery({
    queryKey: ['candidate-imports', jobIds],
    queryFn: async () => Promise.all(jobIds.map(async (jobId) => (await api.imports.job(jobId)).data)),
    enabled: jobIds.length > 0,
    refetchInterval: (query) => (query.state.data || []).every(job => ['completed', 'failed', 'canceled'].includes(job.status)) && query.state.data?.length ? false : 1000,
  });
  const importing = jobIds.length > 0 && !(jobs && jobs.length === jobIds.length && jobs.every(job => ['completed', 'failed', 'canceled'].includes(job.status)));
  const progress = (jobs || []).reduce((sum, job) => ({ done: sum.done + (job.progress?.completed || 0), total: sum.total + (job.progress?.total || 0), errors: sum.errors + (job.progress?.errors || 0) }), { done: 0, total: 0, errors: 0 });
  useEffect(() => {
    if (jobIds.length && !importing) {
      refreshFolderViews(queryClient, folderId);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', folderId] });
      queryClient.invalidateQueries({ queryKey: ['planner-folder', String(folderId)] });
    }
  }, [importing, jobIds.length, folderId, queryClient]);

  const acceptMutation = useMutation({
    mutationFn: async (posts) => {
      const accepted = (await api.planner.acceptCandidates(folderId, posts)).data;
      const started = [];
      for (const [site, remoteIds] of Object.entries(accepted.by_site)) {
        started.push((await api.imports.createBatch({ folder_id: Number(folderId), provider: site, remote_ids: remoteIds })).data.job_id);
      }
      return { accepted, started };
    },
    onSuccess: ({ started }) => {
      setSelected(new Set());
      setJobIds(current => [...current.filter(jobId => (jobs || []).some(job => job.job_id === jobId && !['completed', 'failed', 'canceled'].includes(job.status))), ...started]);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', folderId] });
    },
  });
  const accept = (keys) => {
    if (!keys.length || completed || acceptMutation.isPending) return;
    acceptMutation.mutate(keys.map(key => { const [site, remote_id] = key.split(':'); return { site, remote_id }; }));
  };
  const toggle = (key) => setSelected(current => { const next = new Set(current); next.has(key) ? next.delete(key) : next.add(key); return next; });
  const togglePage = () => setSelected(current => { const next = new Set(current); const all = items.every(item => next.has(keyOf(item))); for (const item of items) all ? next.delete(keyOf(item)) : next.add(keyOf(item)); return next; });

  useEffect(() => {
    if (!hotkeysActive || !open) return undefined;
    const handleKeyDown = (event) => {
      if (document.querySelector('[aria-modal="true"]')) return;
      if (event.target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
      if (event.key.toLowerCase() === 'q') { event.preventDefault(); togglePage(); }
      if (event.key.toLowerCase() === 'w' && selected.size > 0) { event.preventDefault(); accept([...selected]); }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [hotkeysActive, open, items, selected, completed, acceptMutation.isPending]);

  const container = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, [open]);
  const rows = justifiedRows(items, width, tileHeight);
  const { box, hits } = useDragSelect(container, (keys, add) => setSelected((current) => {
    const next = new Set(current);
    keys.forEach((key) => (add ? next.add(key) : next.delete(key)));
    return next;
  }), open && !completed);
  const viewIndex = viewing ? items.findIndex(item => keyOf(item) === viewing) : -1;
  const missing = context.target ? Math.max(0, context.target - context.images) : 0;

  return (
    <section onMouseEnter={() => onHover(true)} onMouseLeave={() => onHover(false)}
      className={`gallery-surface rounded border ${hotkeysActive ? 'border-amber-700/70' : 'border-[#202a34]'}`} aria-label="Planner candidates">
      <div className="flex flex-wrap items-center gap-2 border-b border-[#202a34] p-2 text-xs">
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)} className="font-semibold text-amber-200">{open ? '−' : '+'} Wildcards: not selected by the plan</button>
        <span className="text-slate-400">{data ? `${data.total} ${data.total === 1 ? 'post' : 'posts'}` : ''}{missing ? ` · ${missing} short of the target ${context.target}` : ''}</span>
        {open && <>
          <select value={sort} onChange={(e) => setSort(e.target.value)} className="rounded border border-[#202a34] bg-[#090d12] px-2 py-1"><option value="popular">Most favorited</option><option value="newest">Newest</option><option value="gaps">Fills gaps</option></select>
          <label className="flex items-center gap-1 text-slate-400" title="Posts the plan's blocked tags or quality rules rejected"><input type="checkbox" checked={includeFiltered} onChange={(e) => setIncludeFiltered(e.target.checked)} /> Show filtered</label>
          <label className="flex items-center gap-1 text-slate-400" title="Posts you removed or banned earlier"><input type="checkbox" checked={includeBanned} onChange={(e) => setIncludeBanned(e.target.checked)} /> Show banned</label>
          {context.era_from && <label className="flex items-center gap-1 text-slate-400" title="Hide posts from before the folder's era"><input type="checkbox" checked={eraOnly} onChange={(e) => setEraOnly(e.target.checked)} /> Only {context.era_from}+</label>}
          {selected.size > 0 && <span className="text-amber-200">{selected.size} selected <button type="button" onClick={() => setSelected(new Set())} className="text-slate-400">Clear</button></span>}
          <button type="button" onClick={togglePage} disabled={!items.length || completed} className="rounded bg-[#3a3220] px-2 py-1 text-amber-200 hover:bg-[#4a4029] disabled:opacity-50">{items.length > 0 && items.every(item => selected.has(keyOf(item))) ? 'Deselect page (Q)' : 'Select page (Q)'}</button>
          {selected.size > 0 && <button type="button" onClick={() => accept([...selected])} disabled={acceptMutation.isPending || completed} className="rounded bg-emerald-800 px-2 py-1 font-semibold text-emerald-50 hover:bg-emerald-700 disabled:opacity-50">{acceptMutation.isPending ? 'Accepting...' : `Accept ${selected.size} into the folder (W)`}</button>}
          <span className="ml-auto text-slate-400">{hotkeysActive ? 'Q / W act here' : 'Hover here to use Q / W'}</span>
        </>}
      </div>
      {open && <>
        {completed && <p className="p-2 text-xs text-emerald-300">This folder is accepted as complete. Reopen it to accept more images.</p>}
        {importing && <p role="status" className="p-2 text-xs text-blue-200">Importing accepted posts... {progress.done}/{progress.total}{progress.errors ? ` · ${progress.errors} errors` : ''}</p>}
        {!importing && jobIds.length > 0 && progress.errors > 0 && <p className="p-2 text-xs text-red-300">{progress.errors} accepted posts could not be imported; they stay locked in the planner.</p>}
        {acceptMutation.isError && <p className="p-2 text-xs text-red-300">Accept failed: {acceptMutation.error.response?.data?.detail || acceptMutation.error.message}</p>}
        {error && <p className="p-2 text-xs text-red-300">Could not load candidates: {error.response?.data?.detail || error.message}</p>}
        <div className="flex gap-3 p-2 text-xs text-slate-400">
          <button type="button" disabled={!offset || isFetching} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous page</button>
          <span>{isFetching && !data ? 'Loading...' : `${offset + (items.length ? 1 : 0)} - ${offset + items.length} of ${data?.total || 0}`}</span>
          <button type="button" disabled={data?.next_offset == null || isFetching} onClick={() => setOffset(data.next_offset)}>Next page</button>
        </div>
        <div className="p-2">
          <div ref={container} className="relative select-none space-y-1.5">
            {rows.map((row, rowIndex) => <div key={rowIndex} className="flex gap-1.5" style={{ height: row.height }}>
              {row.items.map(({ image: item, width: tileWidth }) => {
                const key = keyOf(item);
                return (
                  <div key={key} data-select-key={key} role="button" tabIndex={0} aria-label={`Open ${item.site} post ${item.remote_id}`}
                    onClick={(event) => event.shiftKey ? toggle(key) : setViewing(key)}
                    onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); setViewing(key); } }}
                    style={{ width: tileWidth, height: row.height, flexShrink: 0 }}
                    className="relative cursor-pointer overflow-hidden rounded bg-[#0c1219] hover:ring-2 hover:ring-amber-400">
                    <img src={previewUrl(context.artist_id, item, 'grid')} alt={`${item.site} post ${item.remote_id}`} draggable={false} loading="lazy" className="h-full w-full object-contain" />
                    <button type="button" aria-label={`${selected.has(key) ? 'Deselect' : 'Select'} post`} onClick={(event) => { event.stopPropagation(); toggle(key); }}
                      className={`absolute left-2 top-2 z-10 h-4 w-4 rounded border ${selected.has(key) ? 'border-amber-200 bg-amber-500' : 'border-white/60 bg-black/40'}`} />
                    {selected.has(key) && <div className="pointer-events-none absolute inset-0 ring-2 ring-inset ring-amber-400" />}
                    {hits.has(key) && <div className={`pointer-events-none absolute inset-0 ring-2 ring-inset ${box?.remove ? 'bg-rose-500/15 ring-rose-400' : 'bg-amber-300/15 ring-amber-200'}`} />}
                    {item.fills?.length > 0 && <div className="pointer-events-none absolute left-8 right-2 top-2 flex flex-wrap gap-1 text-[10px]">
                      {item.fills.slice(0, 3).map((fill) => <span key={fill.tag} title={`${fill.now} of ${fill.goal} in the dataset`}
                        className={`rounded px-1 ${fill.priority ? 'bg-rose-950/90 text-rose-100 ring-1 ring-rose-600/60' : 'bg-emerald-950/90 text-emerald-100'}`}>+ {fill.priority ? '★ ' : ''}{fill.tag.replaceAll('_', ' ')}</span>)}
                    </div>}
                    <div className="pointer-events-none absolute right-2 top-2 flex flex-col items-end gap-1 text-[10px]">
                      {item.override === 'ban' && <span className="rounded bg-red-950/90 px-1 text-red-200">banned</span>}
                      {item.filtered && <span className="rounded bg-amber-950/90 px-1 text-amber-200">{item.filtered.replaceAll('_', ' ')}</span>}
                      {VIDEO.has((item.ext || '').toLowerCase()) && <span className="rounded bg-black/80 px-1 text-slate-200">{item.ext}</span>}
                    </div>
                    <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent p-2 text-xs text-white">
                      {item.width}x{item.height} · ♥ {item.fav_count ?? item.score ?? 0}{item.year ? ` · ${item.year}` : ''}
                    </div>
                  </div>
                );
              })}
            </div>)}
          {box && <div aria-hidden="true" className={`pointer-events-none absolute z-20 rounded-sm border ${box.remove ? 'border-rose-400 bg-rose-400/10' : 'border-amber-200 bg-amber-200/10'}`}
            style={{ left: box.x, top: box.y, width: box.w, height: box.h, margin: 0 }} />}
          </div>
          {data && !items.length && <p className="p-6 text-center text-sm text-slate-400">No other harvested posts{includeFiltered && includeBanned ? '' : ' (try showing filtered or banned posts)'}.</p>}
        </div>
      </>}
      {viewIndex >= 0 && <CandidateDetail artistId={context.artist_id} item={items[viewIndex]} position={offset + viewIndex + 1} total={data?.total || 0}
        selected={selected.has(viewing)} onToggle={() => toggle(viewing)} canAccept={!completed && !acceptMutation.isPending}
        onAccept={() => { const next = items[viewIndex + 1] || items[viewIndex - 1]; accept([viewing]); setViewing(next ? keyOf(next) : null); }}
        onPrevious={viewIndex > 0 ? () => setViewing(keyOf(items[viewIndex - 1])) : undefined}
        onNext={viewIndex < items.length - 1 ? () => setViewing(keyOf(items[viewIndex + 1])) : undefined}
        onClose={() => setViewing(null)} />}
    </section>
  );
}

function TagList({ title, tags }) {
  if (!tags?.length) return null;
  return (
    <div>
      <h4 className="mb-1 text-xs font-medium uppercase text-slate-400">{title}</h4>
      <div className="flex flex-wrap gap-1">{tags.map(tag => <span key={tag} className="rounded bg-[#1c2530] px-2 py-1 text-xs text-slate-300">{tag.replaceAll('_', ' ')}</span>)}</div>
    </div>
  );
}

function CandidateDetail({ artistId, item, position, total, selected, onToggle, canAccept, onAccept, onPrevious, onNext, onClose }) {
  const dialog = useRef(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [showInfo, setShowInfo] = useState(() => stored('artist.viewerInfo', true));
  useEffect(() => { setLoading(true); setFailed(false); }, [item.site, item.remote_id]);
  const navigation = useRef({});
  navigation.current = { onClose, onPrevious, onNext, onToggle };
  useEffect(() => {
    const previous = document.activeElement;
    const app = document.querySelector('[data-app-shell]');
    if (app) app.inert = true;
    dialog.current?.focus();
    const handleKey = (event) => {
      const { onClose, onPrevious, onNext, onToggle } = navigation.current;
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
      if (event.key === 'ArrowLeft' && onPrevious) { event.preventDefault(); onPrevious(); }
      if (event.key === 'ArrowRight' && onNext) { event.preventDefault(); onNext(); }
      if (event.key.toLowerCase() === 's') { event.preventDefault(); onToggle(); }
    };
    window.addEventListener('keydown', handleKey, true);
    return () => { window.removeEventListener('keydown', handleKey, true); if (app) app.inert = false; if (previous?.isConnected) previous.focus(); };
  }, []);
  const created = item.created_at ? new Date(item.created_at) : null;
  const rows = [['Site', item.site], ['Post', item.remote_id], ['Dimensions', `${item.width} x ${item.height}`], ['Format', (item.ext || '').toUpperCase()],
    ['Rating', item.rating], ['Favorites', item.fav_count ?? '—'], ['Score', item.score ?? '—'],
    ['Posted', created && !Number.isNaN(created.getTime()) ? created.toLocaleDateString() : item.created_at || '—'],
    ['Planner', item.override === 'ban' ? 'Banned' : item.override === 'lock' ? 'Locked' : 'Not selected'],
    ['Filter', item.filtered ? item.filtered.replaceAll('_', ' ') : 'Passes the plan filters']];

  return createPortal(
    <div ref={dialog} role="dialog" aria-modal="true" aria-label="Candidate viewer" tabIndex={-1} className="viewer-atmosphere fixed inset-0 z-50 flex flex-col outline-none">
      <header className="flex flex-wrap items-center gap-3 border-b border-slate-700 px-4 py-2 text-sm">
        <span className="mr-auto">Wildcard {item.site} #{item.remote_id} — {position} of {total}</span>
        <button aria-label="Previous candidate" disabled={!onPrevious} onClick={onPrevious} className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40">Previous</button>
        <button aria-label="Next candidate" disabled={!onNext} onClick={onNext} className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40">Next</button>
        <button onClick={onToggle} className={`rounded border px-3 py-1 ${selected ? 'border-amber-500 text-amber-200' : 'border-slate-700'}`}>{selected ? 'Selected (S)' : 'Select (S)'}</button>
        <button onClick={onAccept} disabled={!canAccept} className="rounded bg-emerald-800 px-3 py-1 font-semibold text-emerald-50 disabled:opacity-40">Accept this one</button>
        <button aria-expanded={showInfo} onClick={() => setShowInfo(value => !value)} className="rounded border border-slate-700 px-3 py-1">{showInfo ? 'Hide information' : 'Show information'}</button>
        <button aria-label="Close candidate viewer" onClick={onClose} className="rounded border border-slate-700 px-3 py-1">Close</button>
      </header>
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <div className="relative flex min-h-0 min-w-0 flex-1 items-center justify-center overflow-auto p-3">
          {loading && !failed && <span role="status" className="absolute left-3 top-3 rounded bg-black/70 p-2 text-xs">Loading preview...</span>}
          {failed && <p role="alert">Preview unavailable. <a href={item.url} target="_blank" rel="noopener noreferrer" className="text-blue-300 underline">Open the post</a></p>}
          <img key={`${item.site}:${item.remote_id}`} src={previewUrl(artistId, item, 'view')} alt={`${item.site} post ${item.remote_id}`}
            onLoad={() => setLoading(false)} onError={() => { setLoading(false); setFailed(true); }}
            className={failed ? 'hidden' : 'max-h-full max-w-full object-contain'} />
        </div>
        <div hidden={!showInfo} className="viewer-info max-h-[50vh] w-full shrink-0 space-y-5 overflow-y-auto border-l border-slate-700 p-6 md:max-h-none md:w-[380px] lg:w-[440px]">
          <h2 className="text-xl font-bold text-slate-100">Post details</h2>
          <p className="text-xs text-slate-400">Preview only (about 1024 px). Accepting downloads the original into the folder.</p>
          <div className="space-y-2 text-sm">{rows.map(([label, value]) => <div key={label} className="flex justify-between gap-3"><span className="text-slate-400">{label}:</span><span className="text-right font-medium">{value}</span></div>)}</div>
          <a href={item.url} target="_blank" rel="noopener noreferrer" className="block text-xs text-blue-300 underline">View on {item.site} →</a>
          <TagList title="Characters" tags={item.characters} />
          <TagList title="Copyrights" tags={item.copyrights} />
          <TagList title="General" tags={item.tags} />
        </div>
      </div>
    </div>, document.body
  );
}

export default PlannerCandidates;
