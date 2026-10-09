import React, { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import { MARKS, applyMark, isActive, marksOf } from '../components/qualityMarks';
import { flagInfo, LEVEL_CLASS, parseFlags } from '../components/analysisFlags';
import CaptionEditor from '../components/CaptionEditor';
import AnalysisDetails from '../components/AnalysisDetails';
import SwipeZoomStage from './SwipeZoomStage';

const VIDEO = /\.(mp4|webm|mkv|mov|m4v)$/i;
export const screenUrl = (image) => backendAssetUrl(`/api/images/${image.id}/screen?size=${typeof window !== 'undefined' && window.devicePixelRatio * window.innerWidth > 1300 ? 1440 : 1080}`);

// Full-screen viewer for touch screens. Swipe sideways to move, down to close; pinch or double-tap to zoom.
// Quick sort (mode "sort"): swipe up removes, sideways keeps and moves on.
export default function TouchViewer({ images, index, setIndex, onClose, folderId, markable, review, mode = 'view', onRemove, onEnd, hasMore, loadMore }) {
  const queryClient = useQueryClient();
  const image = images[index];
  const [sheet, setSheet] = useState(false);
  const [marks, setMarks] = useState(() => marksOf(image));
  const [markError, setMarkError] = useState(null);
  const saveChain = useRef(Promise.resolve());
  const captionDirty = useRef(false);

  useEffect(() => { setMarks(marksOf(image)); setMarkError(null); }, [image?.id]);
  useEffect(() => { // fetch the next pictures ahead of the swipe
    [1, 2, -1].forEach((step) => { const next = images[index + step]; if (next && !VIDEO.test(next.path || '')) new Image().src = screenUrl(next); });
    if (hasMore && index >= images.length - 5) loadMore?.();
  }, [index, images.length]);
  useEffect(() => { // opening an image counts as reviewing it
    if (!markable || !image || image.marks_viewed_at) return;
    api.planner.markViewed(folderId, image.id).then((response) => patchImage(image.id, { marks_viewed_at: response.data.marks_viewed_at })).catch(() => {});
  }, [image?.id]);
  const full = useQuery({ queryKey: ['image', image?.id], enabled: Boolean(sheet && image), queryFn: async () => (await api.images.get(image.id)).data });

  if (!image) return null;

  function patchImage(id, fields) {
    queryClient.setQueriesData({ queryKey: ['collection-images', String(folderId)] }, (old) => {
      if (!old?.pages) return old;
      return { ...old, pages: old.pages.map((page) => ({ ...page, items: page.items.map((item) => (item.id === id ? { ...item, ...fields } : item)) })) };
    });
  }

  const go = (step) => {
    const next = index + step;
    if (next < 0) return;
    if (next >= images.length) { if (hasMore) loadMore?.(); else onEnd?.(); return; }
    setIndex(next);
  };
  const chooseMark = (mark) => {
    if (!markable) return;
    const next = applyMark(mark, marks);
    const touched = mark.axis ? [mark.axis] : ['quality', 'aesthetic'];
    setMarks(next);
    const id = image.id;
    saveChain.current = saveChain.current
      .then(() => api.planner.setMarks(folderId, id, { ...next, touched }))
      .then((response) => { setMarkError(null); patchImage(id, response.data); queryClient.invalidateQueries({ queryKey: ['planner-folder', String(folderId)] }); })
      .catch((error) => setMarkError(`Mark not saved: ${error.response?.data?.detail || error.message}`));
  };
  const remove = () => onRemove?.([image.id]);

  const flags = parseFlags(image.analysis_flags);
  const video = VIDEO.test(image.path || '');

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black text-slate-200" role="dialog" aria-modal="true" aria-label="Image viewer"
      style={{ paddingTop: 'env(safe-area-inset-top, 0px)', paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}>
      <div className="flex items-center gap-2 px-2 py-1">
        <button type="button" aria-label="Close viewer" onClick={onClose} className="h-11 w-11 text-xl">✕</button>
        <span className="text-sm tabular-nums">{index + 1} / {images.length}{hasMore ? '+' : ''}</span>
        {mode === 'sort' && <span className="rounded bg-blue-900/60 px-2 py-0.5 text-[11px] text-blue-100">Quick sort</span>}
        <div className="ml-auto flex flex-wrap justify-end gap-1 text-[10px]">
          {flags.slice(0, 3).map((flag) => { const info = flagInfo(flag); return <span key={flag} className={`rounded px-1 ${LEVEL_CLASS[info.level]}`}>{info.short}</span>; })}
        </div>
      </div>

      <SwipeZoomStage resetKey={image.id} upLabel={mode === 'sort' ? 'Release to remove' : null}
        onSwipe={(direction) => (direction === 'left' ? go(1) : direction === 'right' ? go(-1) : direction === 'up' ? remove() : onClose())}>
        {video
          ? <video src={backendAssetUrl(`/static/images/${image.path}`)} controls playsInline className="max-h-full max-w-full" />
          : <img src={screenUrl(image)} alt={`Image ${image.id}`} draggable={false} className="max-h-full max-w-full object-contain" />}
      </SwipeZoomStage>

      <div className="space-y-2 border-t border-[#202a34] bg-[#0b1016] p-2">
        {markable && <div className="grid grid-cols-3 gap-1.5">
          {MARKS.map((mark) => <button key={mark.label} type="button" onClick={() => chooseMark(mark)}
            className={`min-h-[48px] rounded-md border px-1 py-2.5 text-sm font-medium leading-tight ${isActive(mark, marks) ? mark.active : 'border-slate-700 text-slate-300'}`}>
            {mark.label}</button>)}
        </div>}
        {markError && <p role="alert" className="text-xs text-red-300">{markError}</p>}
        <div className="grid grid-cols-4 gap-2 text-sm">
          <button type="button" onClick={() => go(-1)} disabled={index === 0} className="rounded border border-slate-700 py-3 disabled:opacity-30">◀</button>
          <button type="button" onClick={remove} className="rounded border border-red-800 bg-red-950/50 py-3 text-red-100">{mode === 'sort' ? 'Remove ↑' : 'Remove'}</button>
          <button type="button" onClick={() => setSheet(true)} className="rounded border border-slate-700 py-3">Info</button>
          <button type="button" onClick={() => go(1)} className="rounded border border-slate-700 py-3">{mode === 'sort' ? 'Keep ▶' : '▶'}</button>
        </div>
      </div>

      {sheet && <div className="fixed inset-0 z-[60] flex flex-col justify-end bg-black/50" onClick={(event) => { if (event.target === event.currentTarget) setSheet(false); }}>
        <div className="max-h-[75dvh] overflow-y-auto rounded-t-xl border-t border-[#202a34] bg-[#0d1219] p-3 text-sm" style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 12px)' }}>
          <div className="mb-2 flex items-center justify-between">
            <span className="font-semibold text-slate-100">Image {image.id}</span>
            <button type="button" aria-label="Close details" onClick={() => setSheet(false)} className="h-10 w-10 text-lg">✕</button>
          </div>
          <h3 className="text-xs font-semibold text-slate-400">Tags</h3>
          <p className="mb-3 text-xs leading-relaxed text-slate-300">{full.data ? (full.data.ground_truth_tags || []).join(', ') || 'No tags' : 'Loading…'}</p>
          <div className="mb-3 [overflow-wrap:anywhere]"><CaptionEditor key={image.id} imageId={image.id} dirtyRef={captionDirty} /></div>
          {review?.images?.[image.id] && <AnalysisDetails detail={review.images[image.id]} review={review} />}
        </div>
      </div>}
    </div>
  );
}
