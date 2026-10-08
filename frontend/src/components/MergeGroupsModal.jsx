import React, { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : String(error?.message || 'Request failed');
};

// Confirm merging one sidebar group into another: the dropped-on group keeps its collections and receives the others.
export default function MergeGroupsModal({ source, target, sourceCount, targetCount, onClose }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(target.name);
  const [keepProtection, setKeepProtection] = useState(true);
  const sameSite = source.provider === target.provider;
  const merge = useMutation({
    mutationFn: () => api.groups.merge(source.id, { target_id: target.id, name: name.trim(), keep_protection: keepProtection }),
    onSuccess: () => { queryClient.invalidateQueries(); onClose(); },
  });
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-labelledby="merge-groups-title">
      <form className="w-full max-w-md space-y-4 rounded-lg border border-[#202a34] bg-[#0d1219] p-5 text-sm"
        onSubmit={(event) => { event.preventDefault(); if (sameSite && name.trim()) merge.mutate(); }}>
        <h2 id="merge-groups-title" className="text-base font-semibold text-slate-100">Merge groups</h2>
        <p className="text-slate-300">
          Move all <b>{sourceCount}</b> collections of <b>{source.name}</b> into <b>{target.name}</b> ({targetCount}), then remove the empty
          {' '}<b>{source.name}</b> group. Images, captions, marks and planner links stay with their collections.
        </p>
        {!sameSite && <p role="alert" className="text-amber-300">These groups belong to different sites ({source.provider} and {target.provider}); only groups of the same site can be merged.</p>}
        <label className="block text-xs text-slate-400">Name of the merged group
          <input id="merge-group-name" autoFocus className="mt-1 block w-full rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm text-slate-100"
            value={name} maxLength={100} onChange={(event) => setName(event.target.value)} />
        </label>
        <label className="flex items-start gap-2 text-xs text-slate-300">
          <input id="merge-keep-protection" type="checkbox" className="mt-0.5" checked={keepProtection} onChange={(event) => setKeepProtection(event.target.checked)} />
          <span>Keep protected collections protected from Sync All and group syncs (planner collections are protected).</span>
        </label>
        <p className="text-xs text-slate-500">Folders move on disk under the merged group; nothing is downloaded or deleted. Finish running syncs, imports and QA first.</p>
        {merge.isError && <p role="alert" className="text-red-400">{errorText(merge.error)}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="rounded border border-slate-700 px-3 py-2" onClick={onClose} disabled={merge.isPending}>Cancel</button>
          <button type="submit" className="rounded bg-blue-700 px-3 py-2 text-white disabled:opacity-40" disabled={!sameSite || !name.trim() || merge.isPending}>
            {merge.isPending ? 'Merging…' : 'Merge'}</button>
        </div>
      </form>
    </div>
  );
}
