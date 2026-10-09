import React, { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';
import CaptionEditor from '../components/CaptionEditor';
import { screenUrl } from './TouchViewer';

// Captions on a touch screen: pick which images to go through (flagged by Check captions, missing a caption,
// or all), then step through them with the picture on top and the caption editor below.
export default function TouchCaptions({ folderId, folder }) {
  const [filter, setFilter] = useState('flagged');
  const [index, setIndex] = useState(null);
  const dirty = useRef(false);
  const all = useQuery({
    queryKey: ['collection-images', String(folderId), 'touch-captions'],
    queryFn: async () => {
      const items = [];
      for (let offset = 0; offset < 5000; offset += 200) { // a folder is usually under 100 images
        const page = (await api.dataset.query(folderId, { filters: {}, offset, limit: 200, sort: 'newest' })).data;
        items.push(...(page.items || []));
        if (page.next_cursor == null) break;
      }
      return items;
    },
  });
  const flagged = useQuery({
    queryKey: ['caption-check-items', 'folder', folderId],
    queryFn: async () => (await api.planner.captionCheckItems(null, folderId)).data.items,
  });
  const problems = useMemo(() => new Map((flagged.data || []).map((item) => [item.image_id, item.problems])), [flagged.data]);
  const images = useMemo(() => (all.data || []).filter((image) => (filter === 'flagged' ? problems.has(image.id)
    : filter === 'missing' ? !image.has_caption : true)), [all.data, problems, filter]);
  const counts = { flagged: (all.data || []).filter((image) => problems.has(image.id)).length, missing: (all.data || []).filter((image) => !image.has_caption).length, all: (all.data || []).length };
  const current = index != null ? images[index] : null;
  const move = (step) => {
    if (dirty.current && !window.confirm('Leave this caption without saving?')) return;
    setIndex((value) => Math.max(0, Math.min(images.length - 1, value + step)));
  };

  if (current) return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 px-2 py-1 text-sm text-slate-300">
        <button type="button" aria-label="Back to the list" onClick={() => setIndex(null)} className="h-11 w-11 text-xl">✕</button>
        <span className="tabular-nums">{index + 1} / {images.length}</span>
        {problems.get(current.id) && <span className="truncate text-xs text-amber-300">{problems.get(current.id).join(' · ').replaceAll('_', ' ')}</span>}
      </div>
      <img src={screenUrl(current)} alt={`Image ${current.id}`} className="max-h-[45dvh] w-full bg-black object-contain" />
      <div className="p-2"><CaptionEditor key={current.id} imageId={current.id} dirtyRef={dirty} onSavedNext={() => move(1)} /></div>
      <div className="grid grid-cols-2 gap-2 p-2 text-sm" style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)' }}>
        <button type="button" disabled={index === 0} onClick={() => move(-1)} className="rounded border border-slate-700 py-3 disabled:opacity-30">◀ Previous</button>
        <button type="button" disabled={index >= images.length - 1} onClick={() => move(1)} className="rounded border border-slate-700 py-3 disabled:opacity-30">Next ▶</button>
      </div>
    </div>
  );

  return (
    <div className="space-y-3 p-3 text-sm">
      <div className="flex gap-1">
        {[['flagged', 'Flagged'], ['missing', 'No caption'], ['all', 'All']].map(([key, label]) =>
          <button key={key} type="button" onClick={() => setFilter(key)}
            className={`flex-1 rounded border py-2 text-xs ${filter === key ? 'border-blue-500 text-blue-100' : 'border-slate-700 text-slate-300'}`}>{label} ({counts[key]})</button>)}
      </div>
      {filter === 'flagged' && !flagged.isLoading && counts.flagged === 0 && <p className="text-xs text-slate-400">
        Nothing flagged in {folder.name}. Flags come from Planner → 8. Captioning → Check captions; run it after captioning.</p>}
      {(all.isLoading || flagged.isLoading) && <p className="text-xs text-slate-400">Loading…</p>}
      {images.length > 0 && <button type="button" onClick={() => setIndex(0)} className="w-full rounded bg-blue-700 py-3 text-white">Go through {images.length} captions</button>}
    </div>
  );
}
