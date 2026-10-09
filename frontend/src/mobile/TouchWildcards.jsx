import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import { refreshFolderViews } from '../api/folderCache';

const PAGE = 60;
const keyOf = (item) => `${item.site}:${item.remote_id}`;
const preview = (artistId, item, size) => backendAssetUrl(`/api/planner/previews/${artistId}/${item.site}/${item.remote_id}?size=${size}`);
const DONE = ['completed', 'failed', 'canceled'];

// Wildcards on a touch screen: the artist's harvested posts not in the folder. Tap one to see it large and add it,
// or switch to selecting several; added posts are downloaded into the folder.
export default function TouchWildcards({ folderId, context }) {
  const queryClient = useQueryClient();
  const [sort, setSort] = useState('gaps');
  const [eraOnly, setEraOnly] = useState(true);
  const [limit, setLimit] = useState(PAGE);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [viewing, setViewing] = useState(null);
  const [jobIds, setJobIds] = useState([]);
  const completed = Boolean(context.completed_at);
  const minYear = eraOnly ? context.era_from : null;
  useEffect(() => { setLimit(PAGE); }, [sort, minYear]);

  const candidates = useQuery({
    queryKey: ['planner-candidates', folderId, sort, false, false, 0, minYear, limit],
    queryFn: async () => (await api.planner.candidates(folderId, { sort, include_filtered: false, include_banned: false, offset: 0, limit, min_year: minYear || undefined })).data,
    placeholderData: (previous) => previous,
  });
  const items = candidates.data?.items || [];
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
    mutationFn: async (posts) => {
      const accepted = (await api.planner.acceptCandidates(folderId, posts)).data;
      const started = [];
      for (const [site, remoteIds] of Object.entries(accepted.by_site)) {
        started.push((await api.imports.createBatch({ folder_id: folderId, provider: site, remote_ids: remoteIds })).data.job_id);
      }
      return started;
    },
    onSuccess: (started) => {
      setSelected(new Set()); setSelecting(false); setViewing(null);
      setJobIds((current) => [...current, ...started]);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', folderId] });
    },
  });
  const postsOf = (keys) => keys.map((key) => { const [site, remoteId] = key.split(':'); return { site, remote_id: remoteId }; });
  const toggle = (key) => setSelected((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const progress = (jobs.data || []).reduce((sum, job) => ({ done: sum.done + (job.progress?.completed || 0), total: sum.total + (job.progress?.total || 0) }), { done: 0, total: 0 });
  const viewIndex = viewing ? items.findIndex((item) => keyOf(item) === viewing) : -1;
  const viewed = viewIndex >= 0 ? items[viewIndex] : null;

  return (
    <div className="pb-20">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs">
        <select aria-label="Sort wildcards" value={sort} onChange={(event) => setSort(event.target.value)}
          className="rounded border border-slate-700 bg-[#090d12] px-2 py-2 text-sm">
          <option value="gaps">Fills gaps</option><option value="popular">Most favorited</option><option value="newest">Newest</option>
        </select>
        {context.era_from && <label className="flex items-center gap-1 text-slate-300"><input type="checkbox" checked={eraOnly} onChange={(event) => setEraOnly(event.target.checked)} />From {context.era_from}</label>}
        <button type="button" disabled={completed} onClick={() => { setSelecting(!selecting); setSelected(new Set()); }}
          className={`ml-auto rounded border px-3 py-2 ${selecting ? 'border-blue-500 text-blue-100' : 'border-slate-700 text-slate-300'} disabled:opacity-40`}>{selecting ? 'Cancel' : 'Select several'}</button>
      </div>
      {completed && <p className="px-3 pb-2 text-xs text-amber-300">Reopen the collection to add wildcards.</p>}
      {importing && <p className="px-3 pb-2 text-xs text-blue-200">Downloading added posts{progress.total ? `: ${progress.done} of ${progress.total}` : '…'}</p>}
      {(candidates.error || add.error) && <p role="alert" className="px-3 pb-2 text-xs text-red-400">{(candidates.error || add.error)?.response?.data?.detail || (candidates.error || add.error)?.message}</p>}
      <div className="grid grid-cols-3 gap-0.5 p-0.5 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
        {items.map((item) => {
          const key = keyOf(item);
          const isSelected = selected.has(key);
          return <button key={key} type="button" onClick={() => (selecting ? toggle(key) : setViewing(key))}
            className={`relative aspect-square overflow-hidden bg-[#0d1219] ${isSelected ? 'ring-4 ring-inset ring-blue-500' : ''}`} aria-label={`${item.site} post ${item.remote_id}`}>
            <img src={preview(context.artist_id, item, 'grid')} alt="" loading="lazy" draggable={false} className="h-full w-full object-cover" />
            {isSelected && <span className="absolute left-1 top-1 flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs text-white">✓</span>}
          </button>;
        })}
      </div>
      <div className="p-3 text-center text-xs text-slate-500">
        {candidates.isFetching ? 'Loading…' : items.length === 0 ? 'No wildcards left' : items.length >= limit
          ? <button type="button" onClick={() => setLimit(limit + PAGE)} className="rounded border border-slate-700 px-4 py-2 text-slate-300">Show more</button> : `All ${items.length} wildcards`}
      </div>
      {selecting && <div className="fixed inset-x-0 bottom-0 z-20 flex items-center gap-2 border-t border-[#202a34] bg-[#0b1016] px-3 py-2 text-sm"
        style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)' }}>
        <span className="flex-1 text-slate-300">{selected.size} selected</span>
        <button type="button" disabled={!selected.size || add.isPending} onClick={() => add.mutate(postsOf([...selected]))}
          className="rounded bg-emerald-700 px-4 py-2 text-white disabled:opacity-40">Add to folder</button>
      </div>}
      {viewed && <div className="fixed inset-0 z-50 flex flex-col bg-black" role="dialog" aria-modal="true" aria-label="Wildcard"
        style={{ paddingTop: 'env(safe-area-inset-top, 0px)', paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}>
        <div className="flex items-center gap-2 px-2 py-1 text-sm text-slate-300">
          <button type="button" aria-label="Close" onClick={() => setViewing(null)} className="h-11 w-11 text-xl">✕</button>
          <span className="truncate">{viewed.site} {viewed.remote_id}{viewed.characters ? ` · ${String(viewed.characters).split(' ').slice(0, 3).join(', ')}` : ''}</span>
        </div>
        <div className="flex flex-1 items-center justify-center overflow-hidden">
          <img src={preview(context.artist_id, viewed, 'view')} alt={`${viewed.site} post ${viewed.remote_id}`} className="max-h-full max-w-full object-contain" />
        </div>
        <div className="grid grid-cols-3 gap-2 border-t border-[#202a34] bg-[#0b1016] p-2 text-sm">
          <button type="button" disabled={viewIndex <= 0} onClick={() => setViewing(keyOf(items[viewIndex - 1]))} className="rounded border border-slate-700 py-3 disabled:opacity-30">◀</button>
          <button type="button" disabled={completed || add.isPending} onClick={() => add.mutate(postsOf([viewing]))} className="rounded bg-emerald-700 py-3 text-white disabled:opacity-40">Add to folder</button>
          <button type="button" disabled={viewIndex >= items.length - 1} onClick={() => setViewing(keyOf(items[viewIndex + 1]))} className="rounded border border-slate-700 py-3 disabled:opacity-30">▶</button>
        </div>
      </div>}
    </div>
  );
}
