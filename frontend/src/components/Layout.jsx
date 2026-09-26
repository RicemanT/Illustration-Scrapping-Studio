import React, { useEffect, useState } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ErrorBoundary } from './Feedback';
import Sidebar from './Sidebar';
import SyncAllModal from './SyncAllModal';
import api from '../api/client';
import { reportUI } from '../api/telemetry';
import { refreshFolderViews } from '../api/folderCache';

function Layout() {
  const location = useLocation();
  useEffect(() => { reportUI('navigation', 'Opened ' + location.pathname); }, [location.pathname]);
  const queryClient = useQueryClient();
  const [syncAllJobId, setSyncAllJobId] = useState(null);
  const [showSyncAllOptions, setShowSyncAllOptions] = useState(false);
  const history = useQuery({
    queryKey: ['sync-history-brief'],
    queryFn: async () => (await api.sync.history(10)).data.items,
    refetchInterval: 5000,
  });
  useEffect(() => {
    if (syncAllJobId && !['completed', 'failed', 'canceled'].includes(queryClient.getQueryData(['sync-job', syncAllJobId])?.status)) return;
    const active = history.data?.find((job) => job.kind === 'all' && ['queued', 'running', 'cancelling'].includes(job.status));
    if (active) setSyncAllJobId(active.job_id);
  }, [history.data, syncAllJobId, queryClient]);
  const syncAllJob = useQuery({
    queryKey: ['sync-job', syncAllJobId],
    queryFn: async () => (await api.sync.job(syncAllJobId)).data,
    enabled: Boolean(syncAllJobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status) ? false : 700,
  });
  const syncAll = useMutation({
    mutationFn: (options) => api.sync.all(options),
    onSuccess: (response) => {
      setSyncAllJobId(response.data.job_id);
      setShowSyncAllOptions(false);
    },
  });
  const cancelAll = useMutation({ mutationFn: () => api.sync.cancel(syncAllJobId) });
  const allFinished = ['completed', 'failed', 'canceled'].includes(syncAllJob.data?.status);
  useEffect(() => {
    if (!allFinished) return;
    refreshFolderViews(queryClient);
    queryClient.invalidateQueries({ queryKey: ['sync-history'] });
    queryClient.invalidateQueries({ queryKey: ['sync-history-brief'] });
  }, [allFinished, syncAllJob.data?.job_id, queryClient]);
  const progress = syncAllJob.data?.progress;

  return (
    <div data-app-shell className="app-atmosphere flex h-screen text-slate-300">
      {/* Sidebar */}
      <Sidebar />

      {/* Main Content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top Navigation Bar */}
        <header className="app-topbar border-b border-[#202a34] px-4 py-2">
          <div className="flex items-center justify-between">
            <Link to="/" className="text-sm font-semibold tracking-wide text-slate-200 hover:text-white">
              Illustration Scrapping Studio
            </Link>
            <div className="flex items-center space-x-4">
              <Link to="/" className="px-3 py-1 text-xs border border-[#202a34] rounded hover:bg-[#0c1219]">
                Dashboard
              </Link>
              <button onClick={() => { syncAll.reset(); setShowSyncAllOptions(true); }} disabled={syncAll.isPending || (syncAllJobId && !allFinished)} className="px-3 py-1 text-xs bg-[#273451] text-blue-200 rounded hover:bg-[#354666] disabled:opacity-50">
                {syncAllJobId && !allFinished ? 'Batch running...' : 'Sync All'}
              </button>
              <Link to="/logs" className="text-xs text-slate-300">Logs</Link>
              <Link to="/settings" className="px-3 py-1 text-xs border border-[#202a34] rounded hover:bg-[#0c1219]">
                Settings
              </Link>
            </div>
          </div>
        </header>

        {syncAllJobId && syncAllJob.data && (
          <div className="border-b border-[#202a34] bg-[#0c1219] px-4 py-2 text-xs text-slate-400">
            <div className="flex items-center justify-between gap-3">
              <span>{syncAllJob.data.parameters?.group_name && `${syncAllJob.data.parameters.group_name}: `}{progress?.message || `Sync All ${syncAllJob.data.status}`}</span>
              <div className="flex items-center gap-3">
                <span>{progress?.completed || 0}/{progress?.total || 0} sources · {progress?.new_images || 0} new · {progress?.errors || 0} errors</span>
                {!allFinished && <button type="button" onClick={() => cancelAll.mutate()} className="rounded bg-red-900/70 px-2 py-1 text-red-100">Stop</button>}
                {allFinished && <button type="button" onClick={() => setSyncAllJobId(null)} className="text-slate-400 hover:text-slate-300">Dismiss</button>}
              </div>
            </div>
            <div className="mt-1.5 h-1 overflow-hidden rounded bg-[#0c1219]"><div className="h-full bg-blue-500 transition-all" style={{ width: `${progress?.total ? Math.round((progress.completed / progress.total) * 100) : 5}%` }} /></div>
          </div>
        )}

        {/* Content Area */}
        <main className="app-workspace flex-1 overflow-y-auto p-3">
          <ErrorBoundary key={location.pathname}><Outlet /></ErrorBoundary>
        </main>
      </div>
      {showSyncAllOptions && (
        <SyncAllModal
          onClose={() => { syncAll.reset(); setShowSyncAllOptions(false); }}
          onStart={(options) => syncAll.mutate(options)}
          pending={syncAll.isPending}
          error={syncAll.error}
        />
      )}
    </div>
  );
}

export default Layout;
