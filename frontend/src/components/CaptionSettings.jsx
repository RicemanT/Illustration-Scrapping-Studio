import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

// Which file beside each image holds its natural-language caption.
export default function CaptionSettings() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['caption-settings'], queryFn: async () => (await api.settings.captions()).data });
  const [suffix, setSuffix] = useState('');
  useEffect(() => { if (query.data) setSuffix(query.data.suffix); }, [query.data?.suffix]);
  const save = useMutation({
    meta: { successMessage: 'Caption file ending saved' },
    mutationFn: () => api.settings.updateCaptions({ suffix }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['caption-settings'] });
      queryClient.invalidateQueries({ queryKey: ['caption'] });
      queryClient.invalidateQueries({ queryKey: ['collection-images'] });
    },
  });
  const example = `0a1b2c….jpeg  →  0a1b2c…${suffix || '_nl.txt'}`;
  return (
    <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
      <h2 className="font-semibold text-slate-100">Caption files</h2>
      <p className="mt-1 text-xs text-slate-400">
        Natural-language captions from your captioning script live beside each image and its ground-truth <code>.txt</code>.
        The image viewer shows and edits them, the gallery can filter by them, and they move with their image when it is
        removed, recovered, merged as a duplicate or exported. The app never writes captions on its own.
      </p>
      <form onSubmit={(event) => { event.preventDefault(); save.mutate(); }} className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-400">File-name ending
          <input aria-label="Caption file ending" value={suffix} onChange={(event) => setSuffix(event.target.value)} maxLength={50}
            className="mt-1 block w-40 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 font-mono text-sm text-slate-200" />
        </label>
        <button disabled={save.isPending || !suffix.trim() || suffix === query.data?.suffix} className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">Save</button>
        {query.data && suffix !== query.data.default && <button type="button" onClick={() => setSuffix(query.data.default)} className="rounded border border-slate-700 px-3 py-2 text-xs text-slate-300">Use {query.data.default}</button>}
        <span className="pb-2 font-mono text-[11px] text-slate-500">{example}</span>
      </form>
      <p className="mt-2 text-xs text-slate-400">Changing the ending does not rename existing files; captions with the old ending are no longer shown or moved.</p>
      {save.isError && <p className="mt-2 text-xs text-red-400">{save.error.response?.data?.detail || save.error.message}</p>}
    </section>
  );
}
