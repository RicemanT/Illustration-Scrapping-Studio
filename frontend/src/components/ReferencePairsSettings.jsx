import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

// Exports reference pairs (target, reference, prompt split) for training a MOSS-Image reference adapter.
export default function ReferencePairsSettings() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState([]);
  const [name, setName] = useState('reference-pairs');
  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data });
  const status = useQuery({
    queryKey: ['reference-pairs'],
    queryFn: async () => (await api.settings.referencePairs()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const running = status.data?.status === 'running';
  const start = useMutation({
    mutationFn: () => api.settings.exportReferencePairs({ group_ids: selected, name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reference-pairs'] }),
  });
  const toggle = (id) => setSelected((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  const result = status.data?.result;
  return (
    <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
      <h2 className="font-semibold text-slate-100">Reference pairs (MOSS reference adapter)</h2>
      <p className="mt-1 text-xs text-slate-400">
        Pairs a target image with a reference image for training an adapter that copies a character and style from one
        picture: the same character by the same artist (identity and style), by another artist (identity; the prompt keeps
        the target's artist trigger) and the same artist with other characters (style). Each pair lists the tags the prompt
        keeps and the character's usual look that the reference supplies. A few characters are held out for evaluation.
      </p>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-300">
        {groups.data?.map((group) => (
          <label key={group.id} className="flex items-center gap-1.5">
            <input type="checkbox" checked={selected.includes(group.id)} onChange={() => toggle(group.id)} />{group.name}
          </label>
        ))}
      </div>
      <form onSubmit={(event) => { event.preventDefault(); start.mutate(); }} className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-400">Export name
          <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80}
            className="mt-1 block w-56 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 font-mono text-sm text-slate-200" />
        </label>
        <button disabled={running || start.isPending || !name}
          className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">
          {running ? 'Exporting…' : selected.length ? `Export pairs from ${selected.length} group${selected.length === 1 ? '' : 's'}` : 'Export pairs from every artist folder'}
        </button>
      </form>
      {status.data?.status === 'done' && result && (
        <p className="mt-2 text-xs text-emerald-300">
          {result.pairs.toLocaleString()} pairs from {result.images.toLocaleString()} images in {result.folders} folders
          ({Object.entries(result.by_kind).map(([k, n]) => `${k} ${n.toLocaleString()}`).join(', ')}); held out: {result.held_out_characters.join(', ') || 'none'}.
          Written to {result.path}.
        </p>
      )}
      {status.data?.status === 'error' && <p className="mt-2 text-xs text-red-400">{status.data.error}</p>}
      {start.isError && <p className="mt-2 text-xs text-red-400">{start.error.response?.data?.detail || start.error.message}</p>}
    </section>
  );
}
