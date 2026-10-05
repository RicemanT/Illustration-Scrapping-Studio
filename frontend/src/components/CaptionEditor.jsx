import React, { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const detailOf = (error) => error?.response?.data?.detail || error?.message;

// The natural-language caption file beside an image (for example <name>_nl.txt),
// written by an external captioning script and reviewed/edited here.
// dirtyRef lets the viewer ask before leaving with unsaved edits.
export default function CaptionEditor({ imageId, dirtyRef, onSavedNext }) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['caption', imageId],
    queryFn: async () => (await api.images.caption(imageId)).data,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const data = query.data;
  const [text, setText] = useState(null); // null: showing the file as loaded
  const saved = data?.text ?? '';
  const value = text ?? saved;
  const dirty = text !== null && text !== saved;
  dirtyRef.current = dirty;
  useEffect(() => () => { dirtyRef.current = false; }, [dirtyRef]);
  const area = useRef(null);

  const remember = (caption) => {
    queryClient.setQueryData(['caption', imageId], caption);
    queryClient.setQueriesData({ queryKey: ['collection-images'] }, (old) => {
      if (!old) return old;
      const patch = (items) => items?.map((item) => (item.id === imageId ? { ...item, has_caption: caption.exists } : item));
      return { ...old, items: patch(old.items), images: patch(old.images) };
    });
  };
  const save = useMutation({
    mutationFn: async () => (await api.images.saveCaption(imageId, { text: value, base_version: data?.version ?? null })).data,
    onSuccess: (caption, thenNext) => {
      remember(caption);
      setText(null);
      dirtyRef.current = false;
      if (thenNext === true) onSavedNext?.();
    },
  });
  const remove = useMutation({
    mutationFn: async () => (await api.images.deleteCaption(imageId, data?.version ?? null)).data,
    onSuccess: (caption) => { remember(caption); setText(null); dirtyRef.current = false; },
  });
  const conflict = [save.error, remove.error].some((error) => error?.response?.status === 409);
  const busy = save.isPending || remove.isPending;
  const words = value.trim() ? value.trim().split(/\s+/).length : 0;
  const saveNow = (thenNext = false) => {
    if (busy || data?.error) return;
    if (!dirty) { if (thenNext) onSavedNext?.(); return; }
    save.mutate(thenNext);
  };
  const onKeyDown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveNow(false); }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); saveNow(true); }
  };

  return (
    <div className="border-t border-[#202a34] pt-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-100">Caption</h3>
        {data && <span className="shrink-0 text-xs text-slate-400" title={data.filename}>
          <code className="text-slate-300">{data.suffix}</code>{data.exists || dirty ? ` · ${words} words · ${value.length} chars` : ' · none yet'}
        </span>}
      </div>
      {query.isLoading && <p role="status" className="text-xs text-slate-400">Loading caption…</p>}
      {query.isError && <p role="alert" className="text-xs text-red-300">Caption unavailable: {detailOf(query.error)} <button className="underline" onClick={() => query.refetch()}>Retry</button></p>}
      {data?.error && <p role="alert" className="text-xs text-red-300">{data.error}</p>}
      {data && !data.error && <>
        {!data.exists && text === null && <p className="mb-2 text-xs text-slate-400">No caption file yet. Your captioning script writes <code className="text-slate-300">{data.filename}</code> next to the image; you can also type one here.</p>}
        <textarea ref={area} value={value} onChange={(event) => setText(event.target.value)} onKeyDown={onKeyDown} disabled={busy}
          rows={7} spellCheck placeholder="A natural-language description of the image"
          className={`w-full rounded border p-2 text-sm leading-relaxed text-slate-100 ${dirty ? 'border-amber-600/70' : 'border-[#303d4c]'}`} />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => saveNow(false)} disabled={!dirty || busy} className="rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-50">{save.isPending ? 'Saving…' : 'Save (Ctrl+S)'}</button>
          {onSavedNext && <button type="button" onClick={() => saveNow(true)} disabled={busy} className="rounded border border-[#344a73] px-3 py-1.5 text-xs text-blue-200 disabled:opacity-50" title="Save if changed, then open the next image">Save &amp; next (Ctrl+Enter)</button>}
          {dirty && <button type="button" onClick={() => setText(null)} disabled={busy} className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300">Revert</button>}
          {data.exists && !dirty && <button type="button" disabled={busy}
            onClick={() => { if (window.confirm(`Delete ${data.filename}? A copy is kept in the library's .trash/captions folder.`)) remove.mutate(); }}
            className="ml-auto rounded border border-red-900 px-3 py-1.5 text-xs text-red-300 disabled:opacity-50">Delete file</button>}
        </div>
        {dirty && <p className="mt-1 text-[11px] text-amber-300">Unsaved changes.</p>}
      </>}
      {conflict ? (
        <div role="alert" className="mt-2 rounded border border-amber-700/60 bg-amber-950/40 p-2 text-xs text-amber-200">
          {detailOf(save.error || remove.error)}
          <div className="mt-2 flex gap-2">
            <button type="button" className="rounded border border-amber-600 px-2 py-1" onClick={() => { save.reset(); remove.reset(); query.refetch(); }}>Load the file again, keep my text</button>
            <button type="button" className="rounded border border-slate-600 px-2 py-1" onClick={() => { save.reset(); remove.reset(); setText(null); query.refetch(); }}>Discard my text</button>
          </div>
        </div>
      ) : (save.isError || remove.isError) && <p role="alert" className="mt-2 text-xs text-red-300">{detailOf(save.error || remove.error)}</p>}
    </div>
  );
}
