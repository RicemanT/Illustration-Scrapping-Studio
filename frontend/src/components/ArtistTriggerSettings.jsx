import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const PRESETS = ['@{artist}', 'Drawn by {artist}'];

// Switches the artist trigger (first ground-truth tag and start of each caption) of many artist folders at once.
export default function ArtistTriggerSettings() {
  const queryClient = useQueryClient();
  const [template, setTemplate] = useState('@{artist}');
  const [groupId, setGroupId] = useState('');
  const [captions, setCaptions] = useState(true);
  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data });
  const status = useQuery({
    queryKey: ['artist-trigger'],
    queryFn: async () => (await api.settings.artistTrigger()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const preview = useQuery({
    queryKey: ['artist-trigger-preview', template, groupId],
    queryFn: async () => (await api.settings.artistTriggerPreview(template, groupId || undefined)).data,
    enabled: template.includes('{artist}'),
    retry: false,
  });
  const running = status.data?.status === 'running';
  useEffect(() => {
    if (status.data?.status === 'done') {
      queryClient.invalidateQueries({ queryKey: ['collection-images'] });
      queryClient.invalidateQueries({ queryKey: ['image'] });
      queryClient.invalidateQueries({ queryKey: ['caption'] });
      queryClient.invalidateQueries({ queryKey: ['artist-trigger-preview'] });
    }
  }, [status.data?.status, status.data?.finished_at]);
  const start = useMutation({
    mutationFn: () => api.settings.switchArtistTrigger({ template, group_id: groupId ? Number(groupId) : null, captions }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['artist-trigger'] }),
  });
  const apply = () => {
    const scope = groupId ? `the ${groups.data?.find((g) => g.id === Number(groupId))?.name || 'selected'} group` : 'every artist folder';
    if (window.confirm(`Switch the artist trigger of ${scope} to "${template}"? Tag sidecars${captions ? ' and captions' : ''} are rewritten. Running it again with the other format switches back.`)) start.mutate();
  };
  const result = status.data?.result;
  return (
    <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
      <h2 className="font-semibold text-slate-100">Artist trigger format</h2>
      <p className="mt-1 text-xs text-slate-400">
        The artist trigger is the first ground-truth tag and the start of each natural-language caption. Switching sets the format
        of every artist folder in scope (accepted folders included), rewrites their tag sidecars, and replaces the trigger at the
        start of each caption; captions without one get it added. {'{artist}'} is the folder's artist name.
      </p>
      <form onSubmit={(event) => { event.preventDefault(); apply(); }} className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-400">Format
          <input aria-label="Artist trigger format" value={template} onChange={(event) => setTemplate(event.target.value)} maxLength={200}
            className="mt-1 block w-48 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 font-mono text-sm text-slate-200" />
        </label>
        <div className="flex gap-1.5 pb-1">
          {PRESETS.map((preset) => (
            <button key={preset} type="button" onClick={() => setTemplate(preset)}
              className={`rounded border px-2 py-1 font-mono text-[11px] ${template === preset ? 'border-sky-600 text-sky-200' : 'border-slate-700 text-slate-400'}`}>{preset}</button>
          ))}
        </div>
        <label className="text-xs text-slate-400">Folders
          <select aria-label="Folders to switch" value={groupId} onChange={(event) => setGroupId(event.target.value)}
            className="mt-1 block rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm text-slate-200">
            <option value="">All artist folders</option>
            {groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-2 pb-2 text-xs text-slate-300">
          <input type="checkbox" checked={captions} onChange={(event) => setCaptions(event.target.checked)} /> Also rewrite captions
        </label>
        <button disabled={running || start.isPending || !preview.data || preview.data.folders === 0}
          className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">{running ? 'Switching…' : 'Switch'}</button>
      </form>
      {preview.data && (
        <p className="mt-2 text-xs text-slate-400">
          {preview.data.folders} artist folder{preview.data.folders === 1 ? '' : 's'} · {preview.data.images.toLocaleString()} images
          {preview.data.unchanged_folders ? ` · ${preview.data.unchanged_folders} already use this format` : ''}
          {preview.data.example && <> · e.g. <code className="text-slate-300">{preview.data.example.before || '(none)'}</code> → <code className="text-sky-200">{preview.data.example.after}</code></>}
        </p>
      )}
      {preview.isError && <p className="mt-2 text-xs text-red-400">{preview.error.response?.data?.detail || preview.error.message}</p>}
      {running && <p className="mt-2 text-xs text-sky-300">Switching {status.data.done} / {status.data.total || '…'} folders…</p>}
      {status.data?.status === 'done' && result && (
        <p className="mt-2 text-xs text-emerald-300">
          Switched {result.folders} folders to “{status.data.template}”: {result.sidecars.toLocaleString()} sidecars rewritten
          {status.data.captions ? `, ${result.captions_changed.toLocaleString()} captions changed, ${result.captions_added.toLocaleString()} got the trigger added, ${result.captions_missing.toLocaleString()} images have no caption` : ''}.
        </p>
      )}
      {status.data?.status === 'error' && <p className="mt-2 text-xs text-red-400">{status.data.error}</p>}
      {start.isError && <p className="mt-2 text-xs text-red-400">{start.error.response?.data?.detail || start.error.message}</p>}
    </section>
  );
}
