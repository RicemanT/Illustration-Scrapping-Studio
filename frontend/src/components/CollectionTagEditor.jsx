import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const parseTags = (value) => [...new Set(
  value.split(/[,\n]/).map((tag) => tag.replaceAll('_', ' ').trim().replace(/\s+/g, ' ')).filter(Boolean),
)];

function CollectionTagEditor({ collectionId, selected }) {
  const queryClient = useQueryClient();
  const [input, setInput] = useState('');
  const [replaceFrom, setReplaceFrom] = useState('');
  const [replaceWith, setReplaceWith] = useState('');
  const [activeTagTarget, setActiveTagTarget] = useState('batch');
  const [undoToken, setUndoToken] = useState(null);
  const [message, setMessage] = useState(null);
  const [folderCategories, setFolderCategories] = useState([]);

  const policyQuery = useQuery({
    queryKey: ['folder-tag-category-policy', collectionId],
    queryFn: async () => (await api.tags.getFolderCategoryPolicy(collectionId)).data,
  });
  useEffect(() => {
    if (policyQuery.data) setFolderCategories(policyQuery.data.effective_categories);
  }, [policyQuery.data]);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['collection-tags', collectionId] });
    queryClient.invalidateQueries({ queryKey: ['tag-explorer'] });
    queryClient.invalidateQueries({ queryKey: ['collection-images', String(collectionId)] });
    for (const imageId of selected) queryClient.invalidateQueries({ queryKey: ['image', imageId] });
  };

  const editMutation = useMutation({
    mutationFn: (action) => api.tags.bulkEdit(collectionId, [...selected], parseTags(input), action),
    onSuccess: (response) => {
      const result = response.data;
      setUndoToken(result.undo_token);
      setMessage(`${result.changed_images} of ${result.requested_images} selected images changed.`);
      if (result.changed_images > 0) setInput('');
      refresh();
    },
    onError: (error) => setMessage(error.response?.data?.detail || error.message),
  });

  const replaceMutation = useMutation({
    mutationFn: () => api.tags.bulkReplace(collectionId, [...selected], replaceFrom, replaceWith),
    onSuccess: (response) => {
      const result = response.data;
      setUndoToken(result.undo_token);
      setMessage(`${result.changed_images} of ${result.requested_images} selected images changed (${result.matched_images} contained “${result.old_tag}”).`);
      if (result.changed_images > 0) {
        setReplaceFrom('');
        setReplaceWith('');
      }
      refresh();
    },
    onError: (error) => setMessage(error.response?.data?.detail || error.message),
  });

  const undoMutation = useMutation({
    mutationFn: () => api.tags.undo(undoToken),
    onSuccess: (response) => {
      setMessage(`Restored tags on ${response.data.restored_images} images.`);
      setUndoToken(null);
      refresh();
    },
    onError: (error) => setMessage(error.response?.data?.detail || error.message),
  });
  const policyMutation = useMutation({
    mutationFn: (categories) => api.tags.updateFolderCategoryPolicy(collectionId, categories),
    onSuccess: (response) => {
      setFolderCategories(response.data.effective_categories);
      setMessage(`Ground-truth sections updated on ${response.data.updated_images} images.`);
      queryClient.invalidateQueries({ queryKey: ['folder-tag-category-policy', collectionId] });
      refresh();
    },
    onError: (error) => setMessage(error.response?.data?.detail || error.message),
  });
  const toggleFolderCategory = (category) => {
    const next = folderCategories.includes(category)
      ? folderCategories.filter((item) => item !== category)
      : [...folderCategories, category];
    setFolderCategories(next);
    policyMutation.mutate(next);
  };

  const parsedTags = parseTags(input);
  const disabled = selected.size === 0 || parsedTags.length === 0 || editMutation.isPending;
  const replaceDisabled = selected.size === 0 || !replaceFrom.trim() || !replaceWith.trim() || replaceMutation.isPending;

  const targetBorder = (target) => activeTagTarget === target
    ? 'border-blue-600 ring-1 ring-blue-700/50'
    : 'border-[#202a34]';

  return (
    <div className="p-3 space-y-3">
      <div className="rounded border border-[#202a34] bg-[#0a0f15] p-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-medium text-slate-200">Ground-truth sections for this folder</div>
            <div className="mt-1 text-xs text-slate-400">Excluded sections stay in imported provenance but are removed from this folder's effective tags and sidecars.</div>
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input type="checkbox" checked={Boolean(policyQuery.data?.inherits_global)} onChange={(event) => policyMutation.mutate(event.target.checked ? null : folderCategories)} disabled={policyMutation.isPending} />
            Use global defaults
          </label>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {(policyQuery.data?.available_categories || ['artist', 'character', 'copyright', 'species', 'general', 'meta']).map((category) => (
            <label key={category} className="flex items-center gap-2 rounded border border-[#202a34] bg-[#090d12] px-3 py-1.5 text-xs capitalize">
              <input type="checkbox" checked={folderCategories.includes(category)} onChange={() => toggleFolderCategory(category)} disabled={policyMutation.isPending} />
              {category === 'meta' ? 'metadata' : category}
            </label>
          ))}
        </div>
      </div>
      <div className="rounded border border-[#202a34] bg-[#0a0f15] p-3 space-y-3">
        <div>
          <div className="text-sm font-medium text-slate-200">Batch ground-truth tags</div>
          <div className="mt-1 text-xs text-slate-400">Select images in Gallery using their checkboxes or Q, then add, remove, or replace ground-truth tags. Imported source metadata remains unchanged.</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value.replaceAll('_', ' '))}
            onFocus={() => setActiveTagTarget('batch')}
            placeholder="1girl, blue hair, solo"
            rows={2}
            className={`min-w-64 flex-1 rounded border bg-[#090d12] px-3 py-2 text-sm text-slate-200 placeholder:text-slate-400 ${targetBorder('batch')}`}
          />
          <div className="space-y-2">
            <div className="text-xs text-slate-400">{selected.size} selected images across pages · {parsedTags.length} tags</div>
            <div className="flex gap-2">
              <button type="button" onClick={() => editMutation.mutate('add')} disabled={disabled} className="rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-40">Add to selected</button>
              <button type="button" onClick={() => editMutation.mutate('remove')} disabled={disabled} className="rounded bg-amber-900/70 px-3 py-1.5 text-xs text-amber-200 disabled:opacity-40">Remove from selected</button>
            </div>
          </div>
        </div>
        <div className="border-t border-[#202a34] pt-3">
          <div className="mb-2 text-xs font-medium text-slate-300">Replace one tag across selected images</div>
          <div className="flex flex-wrap items-center gap-2">
            <input aria-label="Tag to replace" value={replaceFrom} onFocus={() => setActiveTagTarget('replaceFrom')} onChange={(event) => setReplaceFrom(event.target.value.replaceAll('_', ' '))} placeholder="diives" className={`min-w-48 flex-1 rounded border bg-[#090d12] px-3 py-2 text-sm text-slate-200 placeholder:text-slate-400 ${targetBorder('replaceFrom')}`} />
            <span className="text-xs text-slate-400">with</span>
            <input aria-label="Replacement tag" value={replaceWith} onFocus={() => setActiveTagTarget('replaceWith')} onChange={(event) => setReplaceWith(event.target.value.replaceAll('_', ' '))} placeholder="Drawn by diives" className={`min-w-48 flex-1 rounded border bg-[#090d12] px-3 py-2 text-sm text-slate-200 placeholder:text-slate-400 ${targetBorder('replaceWith')}`} />
            <button type="button" onClick={() => replaceMutation.mutate()} disabled={replaceDisabled} className="rounded bg-violet-800 px-3 py-2 text-xs text-violet-100 disabled:opacity-40">Replace in selected</button>
          </div>
          <div className="mt-1 text-[11px] text-slate-400">Enter the tag and replacement above. Only matching images are changed; matching ignores capitalization and normalizes underscores to spaces.</div>
        </div>
        <div className="flex items-center gap-3 text-xs">
          {message && <span className="text-slate-400">{message}</span>}
          {undoToken && <button type="button" onClick={() => undoMutation.mutate()} disabled={undoMutation.isPending} className="rounded border border-emerald-800 px-2 py-1 text-emerald-300 disabled:opacity-40">Undo last tag change</button>}
        </div>
      </div>


    </div>
  );
}

export default CollectionTagEditor;
