import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import { refreshFolderViews } from '../api/folderCache';
import { flagInfo, LEVEL_CLASS, parseFlags } from '../components/analysisFlags';
import { markFor } from '../components/qualityMarks';
import TouchViewer from './TouchViewer';
import TouchWildcards from './TouchWildcards';
import TouchCaptions from './TouchCaptions';
import useTouchSelection from './useTouchSelection';

const PAGE = 60;
const errorText = (error) => error?.response?.data?.detail || error?.message || 'Request failed';

// A collection on a phone or tablet: thumbnail grid (flags first), long-press to select and slide to add more
// (the page scrolls by itself near the edges),
// a bottom bar for removing and accepting, a full-screen viewer and quick sort.
export default function TouchFolder() {
  const { id } = useParams();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState('images');
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [viewer, setViewer] = useState(null); // { index, mode }
  const [undo, setUndo] = useState(null); // { token, count }
  const gridRef = useRef(null);
  const sentinel = useRef(null);

  const collection = useQuery({ queryKey: ['collection', id], queryFn: async () => (await api.folders.get(id)).data });
  const { selecting, selected, setSelected, toggle, consumeClick, clear, selectingRef } = useTouchSelection(gridRef, [tab, collection.data?.id]);
  const plannerFolder = useQuery({
    queryKey: ['planner-folder', id], retry: false,
    queryFn: async () => { try { return (await api.planner.folder(id)).data; } catch (error) { if (error.response?.status === 404) return null; throw error; } },
  });
  const planner = plannerFolder.data;
  const review = useQuery({
    queryKey: ['analysis-review', id, collection.data?.image_count], enabled: Boolean(planner), retry: false, staleTime: 60000,
    queryFn: async () => { const data = (await api.analysis.review(id)).data; queryClient.invalidateQueries({ queryKey: ['collection-images', id] }); return data; },
  });
  const sort = review.data?.analysed ? 'flags' : 'newest';
  const pages = useInfiniteQuery({
    queryKey: ['collection-images', id, 'touch', sort, flaggedOnly],
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => (await api.dataset.query(id, { filters: flaggedOnly ? { flagged: true } : {}, offset: pageParam, limit: PAGE, sort })).data,
    getNextPageParam: (last) => (last?.next_cursor != null ? Number(last.next_cursor) : undefined),
  });
  const images = useMemo(() => (pages.data?.pages || []).flatMap((page) => page.items || page.images || []), [pages.data]);
  const total = pages.data?.pages?.[0]?.total ?? collection.data?.image_count ?? 0;

  useEffect(() => { // endless scroll
    const node = sentinel.current;
    if (!node || !pages.hasNextPage) return undefined;
    const observer = new IntersectionObserver((entries) => { if (entries[0].isIntersecting && !pages.isFetchingNextPage) pages.fetchNextPage(); }, { rootMargin: '600px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [pages.hasNextPage, pages.isFetchingNextPage, images.length, tab]);
  useEffect(() => { clear(); setViewer(null); setUndo(null); }, [id]);

  const refresh = () => {
    refreshFolderViews(queryClient, id);
    queryClient.invalidateQueries({ queryKey: ['folder-image-recovery', id] });
    queryClient.invalidateQueries({ queryKey: ['planner-folder', id] });
    queryClient.invalidateQueries({ queryKey: ['tracker-folders'] });
  };
  const remove = useMutation({
    mutationFn: (ids) => api.folders.bulkRemoveImages(id, ids),
    onSuccess: (response, ids) => { setUndo({ token: response.data.undo_token, count: ids.length }); clear(); refresh(); },
  });
  const restore = useMutation({ mutationFn: () => api.folders.undoBulkRemove(id, undo.token), onSuccess: () => { setUndo(null); refresh(); } });
  const complete = useMutation({
    mutationFn: (done) => api.planner.completeFolder(id, done),
    onSuccess: () => { refresh(); queryClient.invalidateQueries({ queryKey: ['planner-candidates', Number(id)] }); },
  });
  useEffect(() => { if (!undo) return undefined; const timer = setTimeout(() => setUndo(null), 10000); return () => clearTimeout(timer); }, [undo]);

  const tap = (image, index) => {
    if (consumeClick()) return;
    if (selectingRef.current) toggle(String(image.id));
    else setViewer({ index, mode: 'view' });
  };

  if (collection.isLoading) return <p className="p-4 text-sm text-slate-400">Loading…</p>;
  if (collection.error) return <p role="alert" className="p-4 text-sm text-red-400">{errorText(collection.error)}</p>;
  const folder = collection.data;
  const accepted = Boolean(planner?.completed_at);
  const flagCount = Object.values(review.data?.counts || {}).reduce((sum, n) => sum + n, 0);

  return (
    <div className="flex min-h-full flex-col">
      <div className="sticky top-0 z-10 space-y-2 border-b border-[#202a34] bg-[#0b1016]/95 px-3 py-2 backdrop-blur">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-slate-100">{folder.name}</h1>
            <p className="text-xs text-slate-400">
              {total} images{planner?.target ? ` · target ${planner.target}` : ''}{accepted ? ' · ' : ''}{accepted && <span className="text-emerald-400">accepted ✓</span>}
            </p>
          </div>
          {planner && <button type="button" disabled={complete.isPending} onClick={() => complete.mutate(!accepted)}
            className={`shrink-0 rounded px-3 py-2 text-sm ${accepted ? 'border border-slate-700 text-slate-300' : 'bg-emerald-700 text-white'}`}>
            {accepted ? 'Reopen' : 'Accept'}</button>}
        </div>
        <div className="flex gap-1 text-sm">
          {[['images', 'Images'], ...(planner ? [['wildcards', 'Wildcards'], ['captions', 'Captions']] : [])].map(([key, label]) =>
            <button key={key} type="button" onClick={() => setTab(key)}
              className={`flex-1 rounded py-2 ${tab === key ? 'bg-[#1b2539] text-blue-100' : 'text-slate-400'}`}>{label}</button>)}
        </div>
        {tab === 'images' && <div className="flex flex-wrap items-center gap-2 text-xs">
          {review.data?.analysed && <button type="button" onClick={() => setFlaggedOnly(!flaggedOnly)}
            className={`rounded-full border px-3 py-1.5 ${flaggedOnly ? 'border-amber-500 text-amber-200' : 'border-slate-700 text-slate-300'}`}>
            {flaggedOnly ? 'Showing flagged' : `Flagged first${flagCount ? ` · ${flagCount} flags` : ''}`}</button>}
          {images.length > 0 && <button type="button" onClick={() => setViewer({ index: 0, mode: 'sort' })}
            className="rounded-full border border-blue-700 px-3 py-1.5 text-blue-200">Quick sort</button>}
          <span className="text-slate-500">Long-press to select</span>
        </div>}
        {complete.isError && <p role="alert" className="text-xs text-red-400">{errorText(complete.error)}</p>}
      </div>

      {tab === 'images' && <>
        <div ref={gridRef} className="grid grid-cols-3 gap-0.5 p-0.5 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {images.map((image, index) => {
            const flags = parseFlags(image.analysis_flags);
            const mark = (image.quality_mark && markFor(image.quality_mark)) || (image.aesthetic_mark && markFor(image.aesthetic_mark));
            const isSelected = selected.has(String(image.id));
            return <button key={image.id} type="button" data-image-id={image.id} data-select-key={image.id} onClick={() => tap(image, index)} onContextMenu={(event) => event.preventDefault()}
              className={`relative aspect-square overflow-hidden bg-[#0d1219] [-webkit-touch-callout:none] ${isSelected ? 'ring-4 ring-inset ring-blue-500' : ''}`}
              aria-label={`Image ${image.id}${isSelected ? ', selected' : ''}`} aria-pressed={selecting ? isSelected : undefined}>
              {image.thumb_path && <img src={backendAssetUrl(`/static/thumbnails/${image.thumb_path}`)} alt="" loading="lazy" draggable={false}
                className="pointer-events-none h-full w-full select-none object-cover" />}
              {flags.length > 0 && <span className={`absolute right-1 top-1 rounded px-1 text-[9px] ${LEVEL_CLASS[flagInfo(flags[0]).level]}`}>{flagInfo(flags[0]).short}</span>}
              {mark && <span className={`absolute bottom-1 left-1 h-2.5 w-2.5 rounded-full ${mark.dot}`} />}
              {isSelected && <span className="absolute left-1 top-1 flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-xs text-white">✓</span>}
            </button>;
          })}
        </div>
        <div ref={sentinel} className="h-16 p-4 text-center text-xs text-slate-500">
          {pages.isFetchingNextPage || pages.isLoading ? 'Loading…' : images.length === 0 ? 'No images' : pages.hasNextPage ? '' : `All ${images.length} images`}
        </div>
      </>}
      {tab === 'wildcards' && planner && <TouchWildcards folderId={Number(id)} context={planner} />}
      {tab === 'captions' && planner && <TouchCaptions folderId={Number(id)} folder={folder} />}

      {tab === 'images' && (selecting || undo) && <div className="sticky bottom-0 z-20 border-t border-[#202a34] bg-[#0b1016] px-2 py-2"
        style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)' }}>
        {selecting ? <div className="flex items-center gap-2 text-sm">
          <span className="flex-1 text-slate-300">{selected.size} selected</span>
          <button type="button" onClick={() => setSelected(new Set(images.map((image) => String(image.id))))} className="rounded border border-slate-700 px-3 py-2">All</button>
          <button type="button" disabled={!selected.size || remove.isPending} onClick={() => remove.mutate([...selected].map(Number))}
            className="rounded bg-red-800 px-3 py-2 text-white disabled:opacity-40">Remove</button>
          <button type="button" onClick={clear} className="rounded border border-slate-700 px-3 py-2">Done</button>
        </div> : <div className="flex items-center gap-2 text-sm">
          <span className="flex-1 text-slate-300">{undo.count} removed</span>
          <button type="button" disabled={restore.isPending} onClick={() => restore.mutate()} className="rounded bg-blue-700 px-4 py-2 text-white">Undo</button>
        </div>}
        {(remove.isError || restore.isError) && <p role="alert" className="mt-1 text-xs text-red-400">{errorText(remove.error || restore.error)}</p>}
      </div>}

      {viewer && images.length > 0 && <TouchViewer images={images} index={Math.min(viewer.index, images.length - 1)} mode={viewer.mode}
        setIndex={(index) => setViewer((current) => ({ ...current, index }))} onClose={() => setViewer(null)}
        folderId={Number(id)} markable={Boolean(planner)} review={review.data}
        hasMore={Boolean(pages.hasNextPage)} loadMore={() => { if (!pages.isFetchingNextPage) pages.fetchNextPage(); }}
        onRemove={(ids) => remove.mutate(ids)}
        onEnd={() => { setViewer(null); if (planner && !accepted && window.confirm('That was the last image. Accept this collection?')) complete.mutate(true); }} />}
      {viewer && undo && <div className="fixed inset-x-2 z-[70] flex items-center gap-2 rounded-lg bg-[#1b2539] px-3 py-2 text-sm shadow-lg" style={{ bottom: 'calc(env(safe-area-inset-bottom, 0px) + 150px)' }}>
        <span className="flex-1">{undo.count} removed</span>
        <button type="button" onClick={() => restore.mutate()} className="rounded bg-blue-700 px-3 py-1.5 text-white">Undo</button>
      </div>}
    </div>
  );
}
