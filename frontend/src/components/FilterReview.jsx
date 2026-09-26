import React, { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
import { control } from './LocalFilters';

export default function FilterReview({ collectionId, filters }) {
  const cache = useQueryClient();
  const [preview, setPreview] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const [undo, setUndo] = useState(null);
  const [status, setStatus] = useState('rejected');
  const load = useMutation({ mutationFn: offset => api.dataset.preview(collectionId, { filters, offset, limit: 100 }), onSuccess: r => { setPreview(r.data); setSelected(new Set()); } });
  const refresh = () => { cache.invalidateQueries({ queryKey: ['collection-images'] }); cache.invalidateQueries({ queryKey: ['tag-explorer'] }); };
  const apply = useMutation({ mutationFn: () => api.dataset.review(collectionId, preview.preview_token, [...selected], status), onSuccess: r => { setUndo(r.data.undo_token); setPreview(null); refresh(); } });
  const restore = useMutation({ mutationFn: () => api.dataset.undo(collectionId, undo), onSuccess: () => { setUndo(null); refresh(); } });
  return <div className="p-3 border-t border-[#202a34] space-y-2 text-xs text-slate-400">
    <button className={control} disabled={load.isPending} onClick={() => load.mutate(0)}>Preview reapply filters</button> <span>View-only until you select and confirm a review action.</span>
    {preview && <div className="space-y-2"><p>{preview.matched} matched · {preview.unmatched} unmatched. {preview.reason}</p><details><summary>Exact previewed filters</summary><pre className="whitespace-pre-wrap">{JSON.stringify(preview.filters, null, 2)}</pre></details>
      <div className="flex flex-wrap max-h-40 overflow-auto gap-2">{preview.items.map(i => <label key={i.id} className={control}><input type="checkbox" checked={selected.has(i.id)} onChange={() => setSelected(current => { const next = new Set(current); next.has(i.id) ? next.delete(i.id) : next.add(i.id); return next; })} /> Image {i.id} · {i.width}×{i.height}</label>)}</div>
      <button className={control} onClick={() => setSelected(new Set(preview.items.map(i => i.id)))}>Select preview page</button> <button className={control} disabled={!preview.offset} onClick={() => load.mutate(Math.max(0,preview.offset-100))}>Previous preview page</button> <button className={control} disabled={!preview.next_cursor} onClick={() => load.mutate(Number(preview.next_cursor))}>Next preview page</button>
      <select className={control} value={status} onChange={e => setStatus(e.target.value)}><option value="rejected">Mark rejected</option><option value="archived">Mark archived</option></select> <button className={control} disabled={!selected.size || apply.isPending} onClick={() => { if (confirm(`Mark only these ${selected.size} previewed images ${status}? Files and tags are retained; this action can be undone.`)) apply.mutate(); }}>Confirm review of {selected.size} selected</button>
    </div>}
    {undo && <button className={control} disabled={restore.isPending} onClick={() => restore.mutate()}>Undo filter review</button>}
    {[load,apply,restore].filter(m => m.isError).map((m,i) => <p key={i} className="text-red-400">{String(m.error.response?.data?.detail || m.error.message)}</p>)}
  </div>;
}
