import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
import { refreshFolderViews } from '../api/folderCache';
import ImportGallery from '../components/ImportGallery';
import ImageGrid from '../components/ImageGrid';
import DuplicateReview from '../components/DuplicateReview';
import CollectionTagEditor from '../components/CollectionTagEditor';
import DatasetTools from '../components/DatasetTools';
import LocalFilters from '../components/LocalFilters';
import TagExplorer from '../components/TagExplorer';
import FilterReview from '../components/FilterReview';
import PlannerCandidates from '../components/PlannerCandidates';
import EraBar from '../components/EraBar';
import { describeQualityJob, useQualityJob } from '../components/qualityJob';
import { ActiveTransferProgress, formatTransferSummary, SyncProgressDetails } from '../components/SyncProgress';

function CollectionView() {
  const { id } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const providerCatalog = useQuery({ queryKey: ['providers'], queryFn: async () => (await api.providers.list()).data });
  const [syncing, setSyncing] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(() => { try { return localStorage.getItem('artist.sourcesOpen') !== 'false'; } catch { return true; } });
  const [tileHeight, setTileHeight] = useState(() => { try { const value = Number(localStorage.getItem('artist.tileHeight')); return value >= 120 && value <= 360 ? value : 220; } catch { return 220; } });
  useEffect(() => { try { localStorage.setItem('artist.sourcesOpen', String(sourcesOpen)); } catch {} }, [sourcesOpen]);
  useEffect(() => { try { localStorage.setItem('artist.tileHeight', String(tileHeight)); } catch {} }, [tileHeight]);
  const [tab, setTab] = useState('gallery');
  const [filters, setFilters] = useState({});
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState('newest');
  const previewAbort = useRef(null);
  const changeFilters = (next) => { setFilters(next); setOffset(0); };
  useEffect(() => { setFilters({}); setOffset(0); setSelected(new Set()); setUndoToken(null); setTab('gallery'); setImportPreview(null); setImportSelected(new Set()); previewAbort.current?.abort(); }, [id]);
  useEffect(() => () => previewAbort.current?.abort(), []);
  const [selected, setSelected] = useState(new Set());
  const [syncJobId, setSyncJobId] = useState(null);
  const [syncLimit, setSyncLimit] = useState(20);
  const [syncSort, setSyncSort] = useState('latest');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [undoToken, setUndoToken] = useState(null);
  const [importProvider, setImportProvider] = useState('danbooru');
  const [importQuery, setImportQuery] = useState('');
  const [importPreview, setImportPreview] = useState(null);
  const [importSelected, setImportSelected] = useState(new Set());
  const [importJobId, setImportJobId] = useState(null);
  const [importBatchId, setImportBatchId] = useState(null);
  const [importCursor, setImportCursor] = useState(null);
  const [importPageCursor, setImportPageCursor] = useState(null);
  const [importCursorHistory, setImportCursorHistory] = useState([]);
  const [importSort, setImportSort] = useState('latest');
  const [importDateFrom, setImportDateFrom] = useState('');
  const [importDateTo, setImportDateTo] = useState('');

  useEffect(() => { previewAbort.current?.abort(); setImportPreview(null); setImportSelected(new Set()); setImportCursor(null); setImportPageCursor(null); setImportCursorHistory([]); }, [importProvider, importQuery, importSort, importDateFrom, importDateTo]);

  const { data: syncJob } = useQuery({
    queryKey: ['sync-job', syncJobId],
    queryFn: async () => (await api.sync.job(syncJobId)).data,
    enabled: Boolean(syncJobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return ['completed', 'failed', 'canceled'].includes(status) ? false : 750;
    },
  });

  const { data: syncHistoryData } = useQuery({
    queryKey: ['sync-history-active-folder'],
    queryFn: async () => (await api.sync.history(20)).data,
    refetchInterval: 750,
  });

  const activeJobs = syncHistoryData?.items?.filter((job) => ['queued', 'running', 'cancelling'].includes(job.status)) || [];
  const activeAllJob = activeJobs.find((job) => job.kind === 'all');
  useEffect(() => {
    const folderId = Number(id);
    const matching = activeJobs.find((job) => (
      (job.kind === 'folder' && Number(job.folder_id) === folderId)
      || (job.kind === 'all' && Number(job.progress?.current_folder_id) === folderId)
    ));
    if (matching && matching.job_id !== syncJobId) setSyncJobId(matching.job_id);
  }, [activeJobs, id, syncJobId]);

  const { data: importJob } = useQuery({
    queryKey: ['import-job', importJobId],
    queryFn: async () => (await api.imports.job(importJobId)).data,
    enabled: Boolean(importJobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status) ? false : 750,
  });

  const { data: collection, isLoading, error: folderError, refetch: reloadFolder } = useQuery({
    queryKey: ['collection', id],
    queryFn: async () => {
      const response = await api.folders.get(id);
      return response.data;
    },
  });

  useEffect(() => {
    if (!collection?.sources?.length) return;
    const selectedSource = collection.sources.find((source) => source.provider === importProvider);
    if (!selectedSource) {
      setImportProvider(collection.sources[0].provider);
      return;
    }
    setImportQuery(selectedSource.query_override || collection.query || '');
  }, [collection?.id, collection?.query, collection?.sources, importProvider]);

  const { data: imagesData, isFetching: imagesLoading, error: imagesError } = useQuery({
    queryKey: ['collection-images', id, filters, offset, sort],
    queryFn: async () => {
      const response = await api.dataset.query(id, { filters, offset, limit: 100, sort });
      return response.data;
    },
  });
  const { data: latestRecoveryData } = useQuery({
    queryKey: ['folder-image-recovery', id],
    queryFn: async () => (await api.folders.latestBulkRecovery(id)).data,
  });
  const availableUndoToken = undoToken || latestRecoveryData?.recovery?.token;
  // Planner collections: target, completion and the wildcard candidates below the gallery.
  const { data: plannerFolder } = useQuery({
    queryKey: ['planner-folder', id],
    queryFn: async () => { try { return (await api.planner.folder(id)).data; } catch (error) { if (error.response?.status === 404) return null; throw error; } },
    retry: false,
  });
  const folderComplete = Boolean(plannerFolder?.completed_at);
  const marks = plannerFolder?.marks;
  const unviewed = marks ? Math.max(0, marks.total - marks.viewed) : 0;
  const markSummary = marks ? ['masterpiece', 'best quality', 'low quality', 'very aesthetic', 'aesthetic'].filter(tag => marks[tag]).map(tag => `${marks[tag]} ${tag}`).join(' · ') : '';
  const quality = useQualityJob();
  const qualityHere = quality.job?.status && quality.job.status !== 'idle' && (quality.job.folder_ids == null || quality.job.folder_ids.includes(Number(id)));
  const [candidatesHotkeys, setCandidatesHotkeys] = useState(false);
  const completeMutation = useMutation({
    meta: { successMessage: 'Folder status saved' },
    mutationFn: (complete) => api.planner.completeFolder(id, complete),
    onSuccess: (response) => {
      queryClient.setQueryData(['planner-folder', id], response.data);
      refreshFolderViews(queryClient, id);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', Number(id)] });
    },
  });
  const toggleComplete = () => {
    if (folderComplete) {
      if (confirm('Reopen this folder? Its images go back to pending and you can remove or accept images again.')) completeMutation.mutate(false);
    } else if (confirm(`Accept this folder as complete? All ${collection.image_count} images become accepted and are locked: future plans select exactly these images, and their quality marks are written into their tags.${unviewed ? `\n\n${unviewed} images were never opened in the viewer; they will be tagged as normal.` : ''}`)) {
      completeMutation.mutate(true);
    }
  };

  const jobFinished = ['completed', 'failed', 'canceled'].includes(syncJob?.status);
  const syncTargetsThisFolder = Boolean(syncJob) && (
    (syncJob.kind === 'folder' && Number(syncJob.folder_id) === Number(id))
    || (syncJob.kind === 'all' && Number(syncJob.progress?.current_folder_id) === Number(id))
  );
  const folderSyncing = syncing || Boolean(activeAllJob) || (syncTargetsThisFolder && !jobFinished);
  useEffect(() => {
    if (syncJob && jobFinished) {
      setSyncing(false);
      refreshFolderViews(queryClient, syncJob.kind === 'all' ? null : syncJob.folder_id || id);
    }
  }, [syncJob, jobFinished, id, queryClient]);

  const images = imagesData?.items || imagesData?.images || [];
  const visibleImages = images;
  const togglePage = () => setSelected(current => { const next = new Set(current); const all = images.every(i => next.has(i.id)); for (const image of images) all ? next.delete(image.id) : next.add(image.id); return next; });
  const confirmDeleteSelected = () => { if (confirm(`Delete ${selected.size} explicitly selected images, including selections on other pages? This replaces the previous recovery batch.`)) bulkRemoveMutation.mutate(); };
  const selectMany = (imageIds) => setSelected((current) => new Set([...current, ...imageIds]));
  const toggleSelected = (imageId) => setSelected((current) => { const next = new Set(current); next.has(imageId) ? next.delete(imageId) : next.add(imageId); return next; });

  const syncMutation = useMutation({
    mutationFn: async ({ provider, limit, options }) => {
      setSyncing(true);
      const response = await api.sync.folder(id, provider, limit, options);
      return response.data;
    },
    onSuccess: (job) => {
      setSyncJobId(job.job_id);
    },
    onError: () => {
      setSyncing(false);
    },
  });

  const cancelSyncMutation = useMutation({
    mutationFn: () => api.sync.cancel(syncJobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['sync-job', syncJobId] }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.folders.delete(id),
    onSuccess: () => {
      refreshFolderViews(queryClient);
      navigate('/');
    },
  });

  const bulkRemoveMutation = useMutation({
    meta: { successMessage: 'Images moved to recovery' },
    mutationFn: () => api.folders.bulkRemoveImages(id, [...selected]),
    onSuccess: (response) => {
      setUndoToken(response.data.undo_token);
      setSelected(new Set());
      refreshFolderViews(queryClient, id);
      queryClient.invalidateQueries({ queryKey: ['folder-image-recovery', id] });
      queryClient.invalidateQueries({ queryKey: ['planner-folder', id] });
    },
  });

  const undoMutation = useMutation({
    meta: { successMessage: 'Deleted images restored' },
    mutationFn: () => api.folders.undoBulkRemove(id, availableUndoToken),
    onSuccess: () => {
      setUndoToken(null);
      refreshFolderViews(queryClient, id);
      queryClient.invalidateQueries({ queryKey: ['folder-image-recovery', id] });
      queryClient.invalidateQueries({ queryKey: ['planner-folder', id] });
    },
  });

  const previewMutation = useMutation({
    mutationFn: async (cursor = null) => { previewAbort.current?.abort(); previewAbort.current = new AbortController(); return (await api.imports.preview({
      folder_id: Number(id),
      provider: importProvider,
      query: importQuery,
      cursor,
      limit: 50,
      sort: importSort,
      date_from: importDateFrom || undefined,
      date_to: importDateTo || undefined,
    }, previewAbort.current.signal)).data; },
    onSuccess: (data, cursor) => {
      setImportPreview(data.items || []);
      setImportCursor(data.next_cursor || null);
      setImportPageCursor(cursor || null);
    },
  });
  const importMutation = useMutation({
    mutationFn: async () => (await api.imports.createBatch({ folder_id: Number(id), provider: importProvider, remote_ids: [...importSelected] })).data,
    onSuccess: (data) => { setImportJobId(data.job_id); setImportBatchId(data.batch_id); },
  });

  const sourceMutation = useMutation({
    meta: { successMessage: 'Source settings saved' },
    mutationFn: ({ provider, ...data }) => api.folders.updateSource(id, provider, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['collection', id] });
      queryClient.invalidateQueries({ queryKey: ['collections'] });
    },
  });

  const cancelImportMutation = useMutation({
    mutationFn: () => api.imports.cancelBatch(importBatchId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['import-job', importJobId] }),
  });

  const importFinished = ['completed', 'failed', 'canceled'].includes(importJob?.status);
  useEffect(() => {
    if (!importJob || !importFinished) return;
    refreshFolderViews(queryClient);
  }, [importJob, importFinished, id, queryClient]);

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (tab !== 'gallery' || candidatesHotkeys || document.querySelector('[aria-modal="true"]')) return;
      if (event.target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
      if (event.key.toLowerCase() === 'q') {
        event.preventDefault();
        const allSelected = images.length > 0 && images.every((image) => selected.has(image.id));
        togglePage();
      }
      if (event.key.toLowerCase() === 'w' && selected.size > 0 && !bulkRemoveMutation.isPending && !folderComplete) {
        event.preventDefault();
        confirmDeleteSelected();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [images, selected.size, bulkRemoveMutation, tab, candidatesHotkeys, folderComplete]);

  const handleSync = (provider) => {
    const limitNum = Math.min(Math.max(parseInt(syncLimit, 10) || 20, 1), 320);
    syncMutation.mutate({ provider, limit: limitNum, options: { sort: syncSort, date_from: dateFrom || undefined, date_to: dateTo || undefined } });
  };

  const handleDelete = () => {
    if (confirm(`Delete folder "${collection.name}"? This cannot be undone.`)) {
      deleteMutation.mutate();
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-slate-400">Loading folder...</div>
      </div>
    );
  }

  if (!collection) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-slate-400">{folderError ? 'Could not load this folder.' : 'Folder not found'} <button onClick={() => reloadFolder()} className="text-blue-300">Retry</button></div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Folder Header */}
      <div className="surface-panel border border-[#202a34] rounded p-3">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-bold text-slate-100">{collection.name} <span className="text-xs font-normal text-slate-400">{collection.image_count} images</span></h1>
            <p className="mt-1 text-slate-400">{collection.query.replaceAll('_', ' ')}</p>
            <div className="mt-2 flex items-center space-x-4 text-sm text-slate-400">
              <span>{collection.image_count} images</span>
              <span className="px-2 py-1 bg-[#1b2539] text-blue-200 rounded">
                {collection.type}
              </span>
            </div>
          </div>
          {plannerFolder && <div className="ml-auto mr-3 flex flex-col items-end gap-1">
            <button type="button" onClick={toggleComplete} disabled={completeMutation.isPending}
              className={folderComplete ? 'rounded-lg border border-emerald-700 px-4 py-2 text-sm text-emerald-200 hover:bg-emerald-950/50 disabled:opacity-50'
                : 'rounded-lg bg-emerald-700 px-6 py-3 text-base font-bold text-white shadow hover:bg-emerald-600 disabled:opacity-50'}>
              {completeMutation.isPending ? 'Saving...' : folderComplete ? 'Reopen folder' : 'Accept folder'}
            </button>
            <span className={`text-xs ${folderComplete ? 'text-emerald-300' : collection.image_count < (plannerFolder.target || 0) ? 'text-amber-300' : 'text-slate-400'}`}>
              {folderComplete ? `Complete since ${new Date(plannerFolder.completed_at).toLocaleDateString()} · ` : 'Pending · '}{collection.image_count}{plannerFolder.target ? ` / ${plannerFolder.target}` : ''} images
            </span>
            {marks && <span className={`text-xs ${!folderComplete && unviewed ? 'text-amber-300' : 'text-slate-400'}`} title={markSummary || 'No quality marks yet'}>
              {unviewed ? `${unviewed} of ${marks.total} not viewed for quality` : `All ${marks.total} viewed for quality`}{markSummary ? ` · ${markSummary}` : ''}{marks.auto ? ` · ${marks.auto} auto` : ''}
            </span>}
            {marks && !folderComplete && <button type="button" onClick={() => quality.start.mutate({ folder_ids: [Number(id)] })}
              disabled={quality.running || quality.start.isPending}
              title="Mark masterpiece / best quality from each post's score percentile (same site, year and rating). Hand-set marks are kept."
              className="rounded border border-amber-700/60 bg-amber-950/30 px-2.5 py-1 text-xs text-amber-100 hover:bg-amber-900/40 disabled:opacity-50">
              {quality.running ? 'Auto quality running…' : 'Auto quality from scores'}
            </button>}
            {qualityHere && <span role="status" className={`max-w-sm text-right text-[11px] ${quality.job.status === 'failed' ? 'text-red-300' : 'text-slate-400'}`}>{describeQualityJob(quality.job)}</span>}
            {quality.start.isError && <span className="text-xs text-red-300">{quality.start.error.response?.data?.detail || quality.start.error.message}</span>}
            {completeMutation.isError && <span className="text-xs text-red-300">{completeMutation.error.response?.data?.detail || completeMutation.error.message}</span>}
          </div>}
          <button
            onClick={handleDelete}
            disabled={deleteMutation.isPending}
            className="px-4 py-2 text-red-300 border border-red-900 rounded-lg hover:bg-red-950/50 disabled:opacity-50"
          >
            {deleteMutation.isPending ? 'Deleting...' : 'Delete Folder'}
          </button>
        </div>

        {/* Sources */}
        <div className="mt-6">
          <button type="button" aria-expanded={sourcesOpen} aria-controls="folder-sources" onClick={() => setSourcesOpen(open => !open)} className="mb-3 flex items-center gap-3 text-sm font-medium text-slate-300">
            <span>{sourcesOpen ? '−' : '+'} Sources</span><span className="text-xs text-slate-400">{collection.sources.filter(source => source.enabled).length} enabled{folderSyncing ? ' · Sync running' : ''}</span>
          </button>
          <div id="folder-sources" hidden={!sourcesOpen}>
            <p className="mb-3 text-xs text-slate-400">Enable any compatible sites here. Disabling a source keeps its images, search and sync history. Configure credentials in Settings before scraping.</p>
            <div className="mb-3 flex flex-wrap items-center gap-2 rounded border border-[#202a34] bg-[#090d12] p-3 text-xs">
              <label className="text-slate-400">Amount <input type="number" min="1" max="320" value={syncLimit} onChange={(e) => setSyncLimit(e.target.value)} className="ml-1 w-16 px-2 py-1 bg-[#090d12] border border-[#202a34] rounded" /></label>
              <label className="text-slate-400">Order <select value={syncSort} onChange={(e) => setSyncSort(e.target.value)} className="ml-1 px-2 py-1 bg-[#090d12] border border-[#202a34] rounded"><option value="latest">Latest</option><option value="oldest">Oldest</option></select></label>
              <label className="text-slate-400">From <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="ml-1 px-2 py-1 bg-[#090d12] border border-[#202a34] rounded" /></label>
              <label className="text-slate-400">To <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="ml-1 px-2 py-1 bg-[#090d12] border border-[#202a34] rounded" /></label>
              {syncTargetsThisFolder && !jobFinished && syncJobId && <button type="button" onClick={() => cancelSyncMutation.mutate()} disabled={cancelSyncMutation.isPending || syncJob?.status === 'cancelling'} className="rounded bg-red-900/70 px-3 py-1.5 text-xs font-semibold text-red-100 hover:bg-red-800 disabled:opacity-50">{syncJob?.status === 'cancelling' || cancelSyncMutation.isPending ? 'Stopping...' : syncJob?.kind === 'all' ? 'Stop batch' : 'Stop sync'}</button>}
            </div>
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {[...new Set([...(providerCatalog.data?.items || []).map(p => p.name), ...collection.sources.map(s => s.provider)])].map(name => {
                const descriptor = providerCatalog.data?.items?.find(p => p.name === name);
                return { provider: name, enabled: false, ...collection.sources.find(s => s.provider === name), supported: (descriptor?.collection_types || ['artist', 'character', 'tag']).includes(collection.type), available: descriptor?.available !== false, reason: descriptor?.unavailable_reason };
              }).map((source) => (
                <div key={source.provider} className="rounded border border-[#202a34] bg-[#0a0f15] p-3">
                  <div className="flex items-center gap-3">
                    <label className="flex min-w-0 flex-1 items-center gap-2 text-sm font-medium text-slate-300">
                      <input type="checkbox" checked={source.enabled} onChange={(event) => sourceMutation.mutate({ provider: source.provider, enabled: event.target.checked })} aria-label={`Enable ${source.provider}`} disabled={sourceMutation.isPending || folderSyncing || !source.supported} />
                      <span className="capitalize">{source.provider === 'twitter' ? 'Twitter / X' : source.provider}</span>
                    </label>
                    {source.last_cursor && <span className="text-[11px] text-slate-400">Cursor {source.last_cursor.substring(0, 8)}...</span>}
                    <button
                      onClick={() => handleSync(source.provider)}
                      disabled={!source.enabled || folderSyncing || !source.available || !source.supported}
                      className="min-w-28 rounded bg-[#344a73] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#405b88] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {syncTargetsThisFolder && !jobFinished ? 'Syncing...' : activeAllJob ? 'Batch running...' : 'Sync now'}
                    </button>
                  </div>
                  {!source.supported && <p className="mt-2 text-xs text-amber-300">Artist profiles only; unavailable for this collection type.</p>}
                  {!source.available && <p className="mt-2 text-xs text-amber-300">{source.reason || 'Configure this provider in Settings before scraping.'}</p>}
                  {(
                    <label className="mt-3 block text-[11px] text-slate-400">
                      {['deviantart', 'pixiv', 'artstation', 'twitter', 'pawchive'].includes(source.provider) ? 'Artist profile URL / ID / handle' : 'Provider-specific search (optional)'}
                      <input
                        key={`${source.provider}:${source.query_override || ''}`}
                        disabled={!source.supported || folderSyncing || sourceMutation.isPending}
                        defaultValue={source.query_override || ''}
                        onBlur={(event) => {
                          if (event.target.value.trim() !== (source.query_override || '')) sourceMutation.mutate({ provider: source.provider, query_override: event.target.value.trim() });
                        }}
                        placeholder={source.provider === 'pawchive' ? 'Full Pawchive creator URL (required)' : collection.query}
                        className="mt-1 block w-full rounded border border-[#202a34] bg-[#090d12] px-2 py-1.5 text-xs text-slate-300"
                      />
                    </label>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>

        {collection.last_sync_at && (
          <div className="mt-4 text-sm text-slate-400">
            Last synced: {new Date(collection.last_sync_at).toLocaleString()}
          </div>
        )}
        {!sourcesOpen && syncTargetsThisFolder && !jobFinished && <div className="mt-2 flex items-center justify-between gap-3 text-xs text-blue-200" role="status">
          <span>{syncJob.progress?.message || 'Sync running...'}</span>
          <button type="button" onClick={() => cancelSyncMutation.mutate()} disabled={cancelSyncMutation.isPending} className="rounded border border-red-900 px-2 py-1 text-red-300">Stop sync</button>
        </div>}
      </div>

      {/* Image Gallery */}
      <div className={`gallery-surface border rounded ${plannerFolder && tab === 'gallery' && !candidatesHotkeys ? 'border-blue-900' : 'border-[#202a34]'}`}>
        <div className="p-2 border-b border-[#202a34] flex flex-wrap items-center gap-2">
          {['gallery','duplicates','tags','import','dataset'].map((name) => <button key={name} onClick={() => setTab(name)} className={`px-3 py-1 text-xs rounded ${tab === name ? 'bg-[#273451] text-blue-200' : 'text-slate-400 hover:text-slate-200'}`}>{name}</button>)}
          {tab === 'gallery' && <>
            <label className="flex items-center gap-2 text-xs text-slate-400">Thumbnail size <input aria-label="Thumbnail size" type="range" min="120" max="360" step="20" value={tileHeight} onChange={event => setTileHeight(Number(event.target.value))} className="w-24 accent-blue-400" /><span className="w-10 tabular-nums">{tileHeight}px</span></label>

            <select value={sort} onChange={(e) => { setSort(e.target.value); setOffset(0); }} className="px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs"><option value="newest">Newest</option><option value="oldest">Oldest</option><option value="posted_newest">Posted (newest)</option><option value="posted_oldest">Posted (oldest)</option><option value="width">Width</option><option value="height">Height</option></select>
            {selected.size > 0 && <span className="text-xs text-blue-300">{selected.size} selected across pages <button onClick={() => setSelected(new Set())}>Clear selection</button></span>}
            <button type="button" onClick={togglePage} disabled={images.length === 0} className="px-2 py-1 text-xs rounded bg-[#273451] text-blue-200 hover:bg-[#354666] disabled:opacity-50">{images.length > 0 && images.every((image) => selected.has(image.id)) ? 'Deselect page (Q)' : 'Select page (Q)'}</button>
            {selected.size > 0 && <button type="button" onClick={confirmDeleteSelected} disabled={bulkRemoveMutation.isPending || folderComplete} title={folderComplete ? 'Reopen the folder to remove images' : undefined} className="px-2 py-1 text-xs rounded bg-red-900/60 text-red-200 hover:bg-red-800 disabled:opacity-50">Delete selected (W)</button>}
            {availableUndoToken && <button type="button" onClick={() => undoMutation.mutate()} disabled={undoMutation.isPending} className="px-2 py-1 text-xs rounded bg-emerald-900/60 text-emerald-200 hover:bg-emerald-800 disabled:opacity-50">Recover last deletion{latestRecoveryData?.recovery?.image_count ? ` (${latestRecoveryData.recovery.image_count})` : ''}</button>}
          </>}
        </div>

        {tab === 'gallery' && plannerFolder && <EraBar folderId={Number(id)} context={plannerFolder} disabled={folderComplete}
          onSelectOlder={(ids) => { selectMany(ids); }} />}
        {['gallery','tags','dataset'].includes(tab) && <LocalFilters filters={filters} onChange={changeFilters} providers={collection.sources.map(s => s.provider)} total={imagesData?.total || 0} qualityFilter={Boolean(plannerFolder)} />}
        {imagesError && <p className="p-3 text-xs text-red-400">Could not query images: {JSON.stringify(imagesError.response?.data?.detail || imagesError.message)}</p>}
        {tab === 'gallery' && <div className="p-2 flex gap-3 text-xs text-slate-400"><button disabled={!offset || imagesLoading} onClick={() => setOffset(Math.max(0,offset-100))}>Previous page</button><span>{imagesLoading ? 'Loading...' : `${offset + (images.length ? 1 : 0)} - ${offset + images.length} of ${imagesData?.total || 0}`}</span><button disabled={!imagesData?.next_cursor || imagesLoading} onClick={() => setOffset(Number(imagesData.next_cursor))}>Next page</button></div>}
        {tab === 'duplicates' ? <DuplicateReview collectionId={Number(id)} /> : tab === 'dataset' ? <DatasetTools key={id} collectionId={Number(id)} filters={filters} onShowImages={ids => { changeFilters({ image_ids: ids }); setTab('gallery'); }} /> : tab === 'gallery' && imagesLoading && !imagesData ? (<div role="status" className="p-4 text-sm text-slate-400">Loading gallery...<div aria-hidden="true" className="mt-3 h-48 rounded bg-slate-800/50 animate-pulse" /></div>) : images.length === 0 && tab === 'gallery' ? (
          <div className="p-12 text-center text-slate-400">
            <p className="mb-4">No images match this view</p>
            <p className="text-sm">Clear local filters to return to the full gallery, or sync sources to add images.</p>
          </div>
        ) : (
          tab === 'gallery' ? <ImageGrid key={id} targetHeight={tileHeight} images={visibleImages} selected={selected} onToggle={toggleSelected}
            onSelectRange={selectMany} eraFrom={plannerFolder?.era_from || null}
            marking={plannerFolder ? { folderId: Number(id), locked: folderComplete } : null}
            onViewerClose={plannerFolder ? () => queryClient.invalidateQueries({ queryKey: ['collection-images', id] }) : undefined} /> : tab === 'tags' ? <><TagExplorer key={`${id}-${JSON.stringify(filters)}`} collectionId={Number(id)} filters={filters} onTag={(tag, exclude) => { const key = exclude ? 'excluded_tags' : 'required_tags'; changeFilters({ ...filters, [key]: [...new Set([...(filters[key] || []), tag])] }); setTab('gallery'); }} /><CollectionTagEditor collectionId={Number(id)} selected={selected} /></> : <div className="p-3 space-y-3">
            <p className="text-xs text-slate-400">Remote metadata discovery. Dimensions are provider hints; quality is checked on decoded originals at import. Local gallery filters do not apply here. Short or empty pages may reflect provider/date limits; use Next page when available.</p>
            {previewMutation.isPending && <button onClick={() => previewAbort.current?.abort()} className="text-xs text-red-300">Cancel preview</button>}
            <div className="flex flex-wrap gap-2 items-center">
              <select value={importProvider} onChange={(e) => setImportProvider(e.target.value)} className="px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs">{collection.sources.map((source) => <option key={source.provider} value={source.provider}>{source.provider}</option>)}</select>
              <input value={importQuery} onChange={(e) => setImportQuery(e.target.value)} placeholder={collection.type === 'tag' ? 'provider tag query' : 'name (spaces work)'} className="flex-1 min-w-48 px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs" />
              <select value={importSort} onChange={(e) => setImportSort(e.target.value)} className="px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs"><option value="latest">Latest</option><option value="oldest">Oldest</option></select>
              <input type="date" value={importDateFrom} onChange={(e) => setImportDateFrom(e.target.value)} className="px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs" />
              <input type="date" value={importDateTo} onChange={(e) => setImportDateTo(e.target.value)} className="px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs" />
              <button type="button" onClick={() => { setImportCursor(null); setImportPageCursor(null); setImportCursorHistory([]); setImportSelected(new Set()); previewMutation.mutate(null); }} disabled={!importQuery.trim() || previewMutation.isPending} className="px-3 py-1 text-xs rounded bg-[#273451] text-blue-200 disabled:opacity-50">{previewMutation.isPending ? 'Searching...' : 'Preview'}</button>
            </div>
            {importPreview && <div className="space-y-2"><div className="flex items-center justify-between text-xs text-slate-400"><span>{importPreview.length} remote results</span><span>{importSelected.size} selected across pages</span></div><ImportGallery key={`${id}-${importProvider}-${importQuery}`} posts={importPreview} selected={importSelected} setSelected={setImportSelected} disabled={importMutation.isPending || Boolean(importJob && !importFinished)} /><div className="flex flex-wrap gap-2"><button type="button" onClick={() => { setImportCursorHistory((history) => [...history, importPageCursor]); previewMutation.mutate(importCursor); }} disabled={!importCursor || previewMutation.isPending} className="px-3 py-1 text-xs rounded border border-[#202a34] text-slate-300 disabled:opacity-50">Next page</button><button type="button" onClick={() => { const previous = importCursorHistory[importCursorHistory.length - 1]; setImportCursorHistory((history) => history.slice(0, -1)); previewMutation.mutate(previous); }} disabled={!importCursorHistory.length || previewMutation.isPending} className="px-3 py-1 text-xs rounded border border-[#202a34] text-slate-300 disabled:opacity-50">Previous page</button><button type="button" onClick={() => importMutation.mutate()} disabled={importSelected.size === 0 || importMutation.isPending} className="px-3 py-1 text-xs rounded bg-[#344a73] text-white disabled:opacity-50">Import selected</button></div></div>}
            {importFinished && <button type="button" onClick={() => previewMutation.mutate(importPageCursor)} disabled={previewMutation.isPending} className="text-xs text-blue-300">Refresh have/new status</button>}
            {previewMutation.isPending && <p role="status" className="text-sm text-slate-400">Searching provider previews...</p>}
            {importJob && <div className="border border-[#202a34] rounded p-2 text-xs text-slate-400"><div className="flex justify-between"><span>Import {importJob.status}{importJob.progress?.workers ? ` · ${importJob.progress.workers} workers` : ''}</span><span>{importJob.progress?.completed || 0}/{importJob.progress?.total || 0}</span></div><ActiveTransferProgress progress={importJob.progress} /><div className="mt-2 h-1.5 bg-[#0c1219] rounded overflow-hidden"><div className="h-full bg-blue-500" style={{ width: `${importJob.progress?.total ? Math.round((importJob.progress.completed / importJob.progress.total) * 100) : 0}%` }} /></div><div className="mt-1">{importJob.progress?.downloaded || 0} downloaded, {importJob.progress?.skipped || 0} skipped, {importJob.progress?.errors || 0} errors</div>{!importFinished && importBatchId && <button type="button" onClick={() => cancelImportMutation.mutate()} disabled={cancelImportMutation.isPending} className="mt-2 px-2 py-1 rounded bg-red-900/60 text-red-200 disabled:opacity-50">Cancel import</button>}{importJob.logs?.length > 0 && <div className="mt-2 max-h-20 overflow-y-auto text-slate-400">{importJob.logs.slice(-4).map((log, index) => <div key={`${log.at}-${index}`} className="flex flex-wrap justify-between gap-x-3"><span>{log.message}</span>{formatTransferSummary(log.current_file) && <span className="text-slate-400">{formatTransferSummary(log.current_file)}</span>}</div>)}</div>}</div>}
            {importMutation.isError && <div className="text-xs text-red-400">Import failed: {importMutation.error.response?.data?.detail || importMutation.error.message}</div>}
            {previewMutation.isError && <div className="text-xs text-red-400">Preview failed: {previewMutation.error.response?.data?.detail || previewMutation.error.message}</div>}
          </div>
        )}
      </div>

      {tab === 'gallery' && plannerFolder && <PlannerCandidates key={id} folderId={Number(id)} context={{ ...plannerFolder, images: collection.image_count }} tileHeight={tileHeight}
        hotkeysActive={candidatesHotkeys} onHover={setCandidatesHotkeys} />}
      {tab === 'gallery' && <FilterReview key={`${id}-${JSON.stringify(filters)}`} collectionId={Number(id)} filters={filters} />}
      {syncMutation.isError && (
        <div className="bg-red-950/50 border border-red-900 rounded-lg p-4 text-red-300">
          Error syncing: {syncMutation.error.response?.data?.detail || syncMutation.error.message}
        </div>
      )}

      {syncJob && syncTargetsThisFolder && (
        <div className="border border-[#202a34] rounded p-3 text-sm text-slate-300">
          <SyncProgressDetails job={syncJob} onCancel={!jobFinished ? () => cancelSyncMutation.mutate() : undefined} canceling={cancelSyncMutation.isPending} />
          {cancelSyncMutation.isError && <div className="mt-2 text-red-400">Could not stop sync: {cancelSyncMutation.error.response?.data?.detail || cancelSyncMutation.error.message}</div>}
        </div>
      )}
    </div>
  );
}

export default CollectionView;
