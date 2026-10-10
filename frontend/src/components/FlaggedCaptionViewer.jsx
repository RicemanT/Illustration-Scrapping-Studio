import React, { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import CaptionEditor from './CaptionEditor';

// One flagged caption next to its image: what the check found, the image's tags, and the caption editor.
// ← / → step through the flagged list (Ctrl+Enter saves and moves on), Esc closes.
export default function FlaggedCaptionViewer({ items, index, setIndex, onClose, labels }) {
  const item = items[index];
  const dirty = useRef(false);
  const image = useQuery({ queryKey: ['image', item?.image_id], enabled: Boolean(item), queryFn: async () => (await api.images.get(item.image_id)).data });

  const leave = (action) => {
    if (dirty.current && !window.confirm('The caption has unsaved changes. Leave without saving?')) return;
    dirty.current = false;
    action();
  };
  const go = (step) => { const next = index + step; if (next >= 0 && next < items.length) leave(() => setIndex(next)); };

  useEffect(() => {
    const onKey = (event) => {
      const typing = ['TEXTAREA', 'INPUT', 'SELECT'].includes(event.target.tagName);
      if (event.key === 'Escape') leave(onClose);
      else if (!typing && event.key === 'ArrowLeft') go(-1);
      else if (!typing && event.key === 'ArrowRight') go(1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  useEffect(() => { // fetch the neighbours ahead
    [items[index + 1], items[index - 1]].forEach((next) => { if (next) new Image().src = backendAssetUrl(`/api/images/${next.image_id}/screen?size=1440`); });
  }, [index]);

  if (!item) return null;
  const details = item.details || {};
  const tags = image.data?.ground_truth_tags || [];
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/90 md:flex-row" role="dialog" aria-modal="true" aria-label="Flagged caption">
      <div className="flex min-h-0 flex-1 items-center justify-center p-2">
        <img src={backendAssetUrl(`/api/images/${item.image_id}/screen?size=1440`)} alt={item.path} className="max-h-full max-w-full object-contain" />
      </div>
      <div className="flex max-h-[55dvh] w-full flex-col gap-3 overflow-y-auto border-t border-slate-800 bg-[#0b1016] p-3 text-sm md:max-h-none md:w-[460px] md:border-l md:border-t-0">
        <div className="flex items-center gap-2">
          <button type="button" className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-30" disabled={index === 0} onClick={() => go(-1)} aria-label="Previous flagged caption">◀</button>
          <span className="tabular-nums text-slate-300">{index + 1} / {items.length}</span>
          <button type="button" className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-30" disabled={index >= items.length - 1} onClick={() => go(1)} aria-label="Next flagged caption">▶</button>
          <button type="button" className="ml-auto rounded border border-slate-700 px-3 py-1.5" onClick={() => leave(onClose)} aria-label="Close">✕</button>
        </div>
        <div className="text-xs">
          <Link className="text-blue-300 underline" to={`/folder/${item.folder_id}`}>{item.folder}</Link>
          <span className="text-slate-500"> · image {item.image_id} · {item.words} words</span>
          <div className="select-all break-all text-slate-500">{item.path}</div>
        </div>
        <div>
          <h4 className="mb-1 text-xs font-semibold text-slate-400">Flagged</h4>
          <ul className="space-y-1">{item.problems.map((code) => <li key={code} className="text-xs">
            <span className="text-amber-300">{labels?.[code] || code}</span>
            {details[code] && <div className="break-words text-slate-300">{details[code]}</div>}
          </li>)}</ul>
          {!item.details && <p className="mt-1 text-xs text-slate-500">Run the check again to see exactly what was found.</p>}
        </div>
        <div>
          <h4 className="mb-1 text-xs font-semibold text-slate-400">Tags</h4>
          <p className="text-xs leading-relaxed text-slate-300">{image.isLoading ? 'Loading…' : tags.join(', ') || 'No tags'}</p>
        </div>
        <div className="[overflow-wrap:anywhere]">
          <CaptionEditor key={item.image_id} imageId={item.image_id} dirtyRef={dirty} shortcutsEverywhere onSavedNext={index < items.length - 1 ? () => setIndex(index + 1) : undefined} />
        </div>
      </div>
    </div>
  );
}
