import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

// Flattens transparent images onto a solid palette colour (one of 12, chosen by each image's hash) and updates their tags.
export default function TransparentSettings() {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState('all');
  const [groupId, setGroupId] = useState('');
  const [folderId, setFolderId] = useState('');
  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data });
  const folders = useQuery({ queryKey: ['collections'], queryFn: async () => (await api.folders.list()).data });
  const status = useQuery({
    queryKey: ['transparent'],
    queryFn: async () => (await api.settings.transparent()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const running = status.data?.status === 'running';
  useEffect(() => {
    if (status.data?.status === 'done') {
      queryClient.invalidateQueries({ queryKey: ['collection-images'] });
      queryClient.invalidateQueries({ queryKey: ['image'] });
    }
  }, [status.data?.status, status.data?.finished_at]);
  const body = scope === 'group' ? { group_id: Number(groupId) } : scope === 'folder' ? { folder_id: Number(folderId) } : {};
  const start = useMutation({
    mutationFn: () => api.settings.flattenTransparent(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['transparent'] }),
  });
  const ready = scope === 'all' || (scope === 'group' && groupId) || (scope === 'folder' && folderId);
  const label = scope === 'group' ? `the ${groups.data?.find((g) => g.id === Number(groupId))?.name || 'selected'} group`
    : scope === 'folder' ? `"${folders.data?.find((f) => f.id === Number(folderId))?.name || 'selected'}"` : 'every folder';
  const apply = () => {
    if (window.confirm(`Flatten the transparent images of ${label} onto solid colours? The files are rewritten (originals kept in the library's .trash/transparent-originals) and their tags updated.`)) start.mutate();
  };
  const result = status.data?.result;
  const field = 'mt-1 block rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm text-slate-200';
  return (
    <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
      <h2 className="font-semibold text-slate-100">Transparent images</h2>
      <p className="mt-1 text-xs text-slate-400">
        Sprite-style images with a transparent background train as noise: dropping the transparency shows whatever colours
        hide under it. Flattening puts each transparent image on a solid background, one of 12 colours (white, black, grey,
        red, blue, green, yellow, pink, purple, orange, brown, aqua) chosen evenly by the image's hash, so no colour
        dominates and a rerun gives the same result. Tags follow: "transparent background" goes, "simple background" and
        "&lt;colour&gt; background" are added. Run it before captioning, so captions describe the new background.
      </p>
      <form onSubmit={(event) => { event.preventDefault(); apply(); }} className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-400">Scope
          <select aria-label="Flatten scope" value={scope} onChange={(event) => setScope(event.target.value)} className={field}>
            <option value="all">All folders</option>
            <option value="group">One group</option>
            <option value="folder">One folder</option>
          </select>
        </label>
        {scope === 'group' && (
          <label className="text-xs text-slate-400">Group
            <select aria-label="Group to flatten" value={groupId} onChange={(event) => setGroupId(event.target.value)} className={field}>
              <option value="">Choose…</option>
              {groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
            </select>
          </label>
        )}
        {scope === 'folder' && (
          <label className="text-xs text-slate-400">Folder
            <select aria-label="Folder to flatten" value={folderId} onChange={(event) => setFolderId(event.target.value)} className={`${field} max-w-xs`}>
              <option value="">Choose…</option>
              {[...(folders.data || [])].sort((a, b) => a.name.localeCompare(b.name)).map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}
            </select>
          </label>
        )}
        <button disabled={running || start.isPending || !ready}
          className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">{running ? 'Flattening…' : 'Flatten transparent images'}</button>
      </form>
      {running && <p className="mt-2 text-xs text-sky-300">Folder {status.data.done} / {status.data.total || '…'} · {(status.data.checked || 0).toLocaleString()} images checked · {(status.data.flattened || 0).toLocaleString()} flattened…</p>}
      {status.data?.status === 'done' && result && (
        <div className="mt-2 text-xs text-emerald-300 space-y-1">
          <p>Checked {result.checked.toLocaleString()} images in {result.folders} folders: {result.flattened.toLocaleString()} flattened
            {result.flattened ? ` (${Object.entries(result.colours).map(([c, n]) => `${c} ${n}`).join(', ')})` : ''}{result.failed ? `, ${result.failed} failed` : ''}.</p>
          {result.captioned > 0 && <p className="text-amber-300">{result.captioned.toLocaleString()} flattened images already had captions, which may still describe a transparent background: caption them again.</p>}
          {result.errors?.length > 0 && <ul className="text-red-300 list-disc ml-4">{result.errors.map((e) => <li key={e}>{e}</li>)}</ul>}
        </div>
      )}
      {status.data?.status === 'error' && <p className="mt-2 text-xs text-red-400">{status.data.error}</p>}
      {start.isError && <p className="mt-2 text-xs text-red-400">{start.error.response?.data?.detail || start.error.message}</p>}
    </section>
  );
}
