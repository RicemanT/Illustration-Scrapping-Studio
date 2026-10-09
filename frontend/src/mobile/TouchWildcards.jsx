import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import { refreshFolderViews } from '../api/folderCache';
import SwipeZoomStage from './SwipeZoomStage';
import useTouchSelection from './useTouchSelection';

const PAGE = 60;
const keyOf = (item) => `${item.site}:${item.remote_id}`;
const preview = (artistId, item, size) => backendAssetUrl(`/api/planner/previews/${artistId}/${item.site}/${item.remote_id}?size=${size}`);
const DONE = ['completed', 'failed', 'canceled'];

// Wildcards on a touch screen: the artist's harvested posts not in the folder. Works like the image grid:
// long-press and slide to select several, tap to open one large (swipe, zoom; swipe up adds it). Added posts are
// downloaded into the folder.
export default function TouchWildcards({ folderId, context }) {
  const queryClient = useQueryClient();
  const [sort, setSort] = useState('gaps');
  const [eraOnly, setEraOnly] = useState(true);
  const [limit, setLimit] = useState(PAGE);
  const [viewing, setViewing] = useState(null); // index into items
  const [added, setAdded] = useState(() => new Set());
  const [jobIds, setJobIds] = useState([]);
  const gridRef = useRef(null);
  const sentinel = useRef(null);
  const { selecting, selected, setSelected, toggle, consumeClick, clear, selectingRef } = useTouchSelection(gridRef);
  const completed = Boolean(context.completed_at);
  const minYear = eraOnly ? context.era_from : null;
  useEffect(() => { setLimit(PAGE); }, [sort, minYear]);

  const candidates = useQuery({
    queryKey: ['planner-candidates', folderId, sort, false, false, 0, minYear, limit],
    queryFn: async () => (await api.planner.candidates(folderId, { sort, include_filtered: false, include_banned: false, offset: 0, limit, min_year: minYear || undefined })).data,
    placeholderData: (previous) => previous,
  });
  const raw = candidates.data?.items || [];
  const items = useMemo(() => raw.filter((item) => !added.has(keyOf(item))), [raw, added]);
  const hasMore = raw.length >= limit;
  const loadMore = () => { if (hasMore && !candidates.isFetching) setLimit(limit + PAGE); };

  useEffect(() => { // endless scroll, like the image grid
    const node = sentinel.current;
    if (!node || !hasMore) return undefined;
    const observer = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMore(); }, { rootMargin: '600px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, candidates.isFetching, limit]);

  const jobs = useQuery({
    queryKey: ['candidate-imports', jobIds], enabled: jobIds.length > 0,
    queryFn: async () => Promise.all(jobIds.map(async (jobId) => (await api.imports.job(jobId)).data)),
    refetchInterval: (query) => ((query.state.data || []).length && query.state.data.every((job) => DONE.includes(job.status)) ? false : 1500),
  });
  const importing = jobIds.length > 0 && !(jobs.data?.length === jobIds.length && jobs.data.every((job) => DONE.includes(job.status)));
  useEffect(() => {
    if (jobIds.length && !importing) {
      refreshFolderViews(queryClient, folderId);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', folderId] });
      queryClient.invalidateQueries({ queryKey: ['planner-folder', String(folderId)] });
    }
  }, [importing, jobIds.length]);
  const add = useMutation({
    mutationFn: async (keys) => {
      const posts = keys.map((key) => { const [site, remoteId] = key.split(':'); return { site, remote_id: remoteId }; });
      const accepted = (await api.planner.acceptCandidates(folderId, posts)).data;
      const started = [];
      for (const [site, remoteIds] of Object.entries(accepted.by_site)) {
        started.push((await api.imports.createBatch({ folder_id: folderId, provider: site, remote_ids: remoteIds })).data.job_id);
      }
      return started;
    },
    onSuccess: (started, keys) => {
      clear();
      setAdded((current) => new Set([...current, ...keys]));
      setJobIds((current) => [...current, ...started]);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', folderId] });
    },
  });
  const progress = (jobs.data || []).reduce((sum, job) => ({ done: sum.done + (job.progress?.completed || 0), total: sum.total + (job.progress?.total || 0) }), { done: 0, total: 0 });

  const tap = (item, index) => {
    if (consumeClick()) return;
    if (selectingRef.current) toggle(keyOf(item));
    else setViewing(index);
  };

  // The added post drops out of the list, so the viewer stays on the same index and shows the next one.
  const viewIndex = viewing == null ? -1 : Math.min(viewing, items.length - 1);
  const viewed = viewIndex >= 0 ? items[viewIndex] : null;
  useEffect(() => { if (viewing != null && !items.length && !candidates.isFetching) setViewing(null); }, [viewing, items.length, candidates.isFetching]);
  useEffect(() => { // fetch the next pictures ahead of the swipe
    if (viewIndex < 0) return;
    [1, 2, -1].forEach((step) => { const next = items[viewIndex + step]; if (next) new Image().src = preview(context.artist_id, next, 'view'); });
    if (viewIndex >= items.length - 5) loadMore();
  }, [viewIndex, items.length]);
  const go = (step) => {
    const next = viewIndex + step;
    if (next < 0) return;
    if (next >= items.length) { if (hasMore) loadMore(); else setViewing(null); return; }
    setViewing(next);
  };
  const addViewed = () => { if (viewed && !completed && !add.isPending) add.mutate([keyOf(viewed)]); };

  return (
    <div className="pb-20">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs">
        <select aria-label="Sort wildcards" value={sort} onChange={(event) => setSort(event.target.value)}
          className="rounded border border-slate-700 bg-[#090d12] px-2 py-2 text-sm">
          <option value="gaps">Fills gaps</option><option value="popular">Most favorited</option><option value="newest">Newest</option>
        </select>
        {context.era_from && <label className="flex items-center gap-1 text-slate-300"><input type="checkbox" checked={eraOnly} onChange={(event) => setEraOnly(event.target.checked)} />From {context.era_from}</label>}
        <span className="ml-auto text-slate-500">Long-press to select</span>
      </div>
      {completed && <p className="px-3 pb-2 text-xs text-amber-300">Reopen the collection to add wildcards.</p>}
      {importing && <p className="px-3 pb-2 text-xs text-blue-200">Downloading added posts{progress.total ? `: ${progress.done} of ${progress.total}` : '…'}</p>}
      {(candidates.error || add.error) && <p role="alert" className="px-3 pb-2 text-xs text-red-400">{(candidates.error || add.error)?.response?.data?.detail || (candidates.error || add.error)?.message}</p>}
      <div ref={gridRef} className="grid grid-cols-3 gap-0.5 p-0.5 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
        {items.map((item, index) => {
          const key = keyOf(item);
          const isSelected = selected.has(key);
          return <button key={key} type="button" data-select-key={key} data-wildcard={key} onClick={() => tap(item, index)} onContextMenu={(event) => event.preventDefault()}
            className={`relative aspect-square overflow-hidden bg-[#0d1219] [-webkit-touch-callout:none] ${isSelected ? 'ring-4 ring-inset ring-blue-500' : ''}`}
            aria-label={`${item.site} post ${item.remote_id}${isSelected ? ', selected' : ''}`} aria-pressed={selecting ? isSelected : undefined}>
            <img src={preview(context.artist_id, item, 'grid')} alt="" loading="lazy" draggable={false} className="pointer-events-none h-full w-full select-none object-cover" />
            {isSelected && <span className="absolute left-1 top-1 flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs text-white">✓</span>}
          </button>;
        })}
      </div>
      <div ref={sentinel} className="h-16 p-4 text-center text-xs text-slate-500">
        {candidates.isFetching ? 'Loading…' : items.length === 0 ? 'No wildcards left' : hasMore ? '' : `All ${items.length} wildcards`}
      </div>
      {selecting && <div className="fixed inset-x-0 bottom-0 z-20 flex items-center gap-2 border-t border-[#202a34] bg-[#0b1016] px-2 py-2 text-sm"
        style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)' }}>
        <span className="flex-1 text-slate-300">{selected.size} selected</span>
        <button type="button" onClick={() => setSelected(new Set(items.map(keyOf)))} className="rounded border border-slate-700 px-3 py-2">All</button>
        <button type="button" disabled={!selected.size || completed || add.isPending} onClick={() => add.mutate([...selected])}
          className="rounded bg-emerald-700 px-3 py-2 text-white disabled:opacity-40">Add</button>
        <button type="button" onClick={clear} className="rounded border border-slate-700 px-3 py-2">Done</button>
      </div>}
      {viewed && <div className="fixed inset-0 z-50 flex flex-col bg-black text-slate-200" role="dialog" aria-modal="true" aria-label="Wildcard viewer"
        style={{ paddingTop: 'env(safe-area-inset-top, 0px)', paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}>
        <div className="flex items-center gap-2 px-2 py-1 text-sm">
          <button type="button" aria-label="Close viewer" onClick={() => setViewing(null)} className="h-11 w-11 shrink-0 text-xl">✕</button>
          <span className="shrink-0 tabular-nums">{viewIndex + 1} / {items.length}{hasMore ? '+' : ''}</span>
          <span className="min-w-0 truncate text-xs text-slate-400">{viewed.site} {viewed.remote_id}{viewed.characters ? ` · ${String(viewed.characters).split(' ').slice(0, 3).join(', ')}` : ''}</span>
        </div>
        <SwipeZoomStage resetKey={keyOf(viewed)} upLabel={completed ? null : 'Release to add'}
          onSwipe={(direction) => (direction === 'left' ? go(1) : direction === 'right' ? go(-1) : direction === 'up' ? addViewed() : setViewing(null))}>
          <img src={preview(context.artist_id, viewed, 'view')} alt={`${viewed.site} post ${viewed.remote_id}`} draggable={false} className="max-h-full max-w-full object-contain" />
        </SwipeZoomStage>
        <div className="grid grid-cols-3 gap-2 border-t border-[#202a34] bg-[#0b1016] p-2 text-sm">
          <button type="button" disabled={viewIndex <= 0} onClick={() => go(-1)} className="rounded border border-slate-700 py-3 disabled:opacity-30">◀</button>
          <button type="button" disabled={completed || add.isPending} onClick={addViewed} className="rounded bg-emerald-700 py-3 text-white disabled:opacity-40">Add ↑</button>
          <button type="button" onClick={() => go(1)} className="rounded border border-slate-700 py-3">▶</button>
        </div>
      </div>}
    </div>
  );
}
