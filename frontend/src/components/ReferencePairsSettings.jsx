import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

// Exports reference pairs (target, reference, prompt split) for training a MOSS-Image reference adapter.
export default function ReferencePairsSettings() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState([]);
  const [name, setName] = useState(() => `pairs-${new Date().toISOString().slice(0, 10)}`);
  const [acceptedOnly, setAcceptedOnly] = useState(true);
  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data });
  const status = useQuery({
    queryKey: ['reference-pairs'],
    queryFn: async () => (await api.settings.referencePairs()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const finished = status.data?.finished_at;
  React.useEffect(() => { if (finished) queryClient.invalidateQueries({ queryKey: ['reference-pairs-history'] }); }, [finished, queryClient]);
  const running = status.data?.status === 'running';
  const start = useMutation({
    mutationFn: () => api.settings.exportReferencePairs({ group_ids: selected, name, accepted_only: acceptedOnly }),
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
        Export now and then under a new name (the date) to see how curation moves the numbers.
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
        <label className="flex items-center gap-1.5 pb-2 text-xs text-slate-300" title="Only folders accepted in the Tracker: the curated part of the selection">
          <input type="checkbox" checked={acceptedOnly} onChange={(event) => setAcceptedOnly(event.target.checked)} />Accepted folders only
        </label>
        <button disabled={running || start.isPending || !name}
          className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">
          {running ? 'Exporting…' : `Export pairs from ${acceptedOnly ? 'accepted folders in ' : ''}${selected.length ? `${selected.length} group${selected.length === 1 ? '' : 's'}` : 'every group'}`}
        </button>
      </form>
      {running && status.data?.progress && <p className="mt-2 text-xs text-slate-400">{status.data.progress}…</p>}
      {status.data?.status === 'done' && result && (
        <div className="mt-2 space-y-1 text-xs text-emerald-300">
          <p>
            {result.pairs.toLocaleString()} pairs from {result.images.toLocaleString()} images in {result.folders.toLocaleString()}
            {result.accepted_only ? ' accepted' : ''} folders ({Object.entries(result.by_kind).map(([k, n]) => `${k} ${n.toLocaleString()}`).join(', ')}).
          </p>
          {result.coverage && <p>
            Characters: {result.coverage.characters.toLocaleString()} ({result.coverage.characters_2_plus_images.toLocaleString()} with 2+ images,{' '}
            {result.coverage.characters_2_plus_artists.toLocaleString()} drawn by 2+ artists, {result.coverage.characters_5_plus_artists.toLocaleString()} by 5+)
            across {result.coverage.artists.toLocaleString()} artists.
          </p>}
          <p>Held out ({result.held_out_characters.length}): {result.held_out_characters.join(', ') || 'none'}. Written to {result.path}.</p>
        </div>
      )}
      {status.data?.status === 'error' && <p className="mt-2 text-xs text-red-400">{status.data.error}</p>}
      {start.isError && <p className="mt-2 text-xs text-red-400">{start.error.response?.data?.detail || start.error.message}</p>}
    </section>
  );
}
