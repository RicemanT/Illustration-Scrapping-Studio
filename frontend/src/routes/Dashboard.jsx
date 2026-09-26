import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import api from '../api/client';
import { SyncProgressDetails } from '../components/SyncProgress';

function Dashboard() {
  const { data: collectionsData, isLoading } = useQuery({
    queryKey: ['collections'],
    queryFn: async () => {
      const response = await api.folders.list();
      return response.data;
    },
  });
  const { data: syncHistoryData } = useQuery({
    queryKey: ['sync-history'],
    queryFn: async () => (await api.sync.history(20)).data,
    refetchInterval: (query) => query.state.data?.items?.some((job) => ['queued', 'running', 'cancelling'].includes(job.status)) ? 1000 : false,
  });

  const collections = collectionsData || [];
  const totalImages = collections.reduce((sum, c) => sum + c.image_count, 0);
  const hasActiveSync = syncHistoryData?.items?.some((job) => ['queued', 'running', 'cancelling'].includes(job.status));

  return (
    <div className="space-y-3">
      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="bg-[#10161e] border border-[#202a34] rounded p-4">
          <h3 className="text-sm font-medium text-slate-400">Total Folders</h3>
          <p className="mt-2 text-3xl font-bold text-slate-100">{collections.length}</p>
        </div>

        <div className="bg-[#10161e] border border-[#202a34] rounded p-4">
          <h3 className="text-sm font-medium text-slate-400">Total Images</h3>
          <p className="mt-2 text-3xl font-bold text-slate-100">{totalImages}</p>
        </div>

        <div className="bg-[#10161e] border border-[#202a34] rounded p-4">
          <h3 className="text-sm font-medium text-slate-400">Status</h3>
          <p className={`mt-2 text-lg font-semibold ${hasActiveSync ? 'text-blue-400' : 'text-green-600'}`}>{hasActiveSync ? 'Syncing' : 'Ready'}</p>
        </div>
      </div>

      <div className="bg-[#0c1219] border border-[#202a34] rounded">
        <div className="p-4 border-b border-[#202a34] flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">Sync history</h2>
          <span className="text-xs text-slate-400">Durable across backend restarts</span>
        </div>
        {!syncHistoryData?.items?.length ? <div className="p-4 text-sm text-slate-400">No sync jobs yet.</div> : (
          <div className="divide-y divide-[#202a34]">
            {syncHistoryData.items.slice(0, 10).map((job) => {
              const result = job.result || {};
              const progress = job.progress || {};
              const active = ['queued', 'running', 'cancelling'].includes(job.status);
              return <div key={job.job_id} className="grid gap-2 px-4 py-3 text-xs sm:grid-cols-[1.4fr_.7fr_1fr_1fr]">
                <div><span className="text-slate-200">{job.kind === 'all' ? (job.parameters?.group_name ? `Group: ${job.parameters.group_name} / ${job.parameters.provider}` : 'Sync All') : `Folder ${job.folder_id} / ${job.provider}`}</span><span className="ml-2 text-slate-400">{job.trigger}</span></div>
                <span className={job.status === 'completed' ? 'text-emerald-400' : job.status === 'failed' ? 'text-red-400' : job.status === 'canceled' ? 'text-amber-400' : 'text-blue-400'}>{job.status}</span>
                <span className="text-slate-400">{result.new_images ?? progress.new_images ?? 0} new · {result.errors ?? progress.errors ?? 0} errors</span>
                <span className="text-slate-400">{new Date(job.started_at || job.created_at).toLocaleString()}</span>
                {active && (
                  <div className="col-span-full mt-1 rounded border border-[#243244] bg-[#0a0f15] p-3 text-slate-300">
                    <SyncProgressDetails job={job} showResult={false} />
                  </div>
                )}
              </div>;
            })}
          </div>
        )}
      </div>

      {/* Folders Grid */}
      <div className="bg-[#0c1219] border border-[#202a34] rounded">
        <div className="p-4 border-b border-[#202a34]">
          <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">Folders</h2>
        </div>

        {isLoading ? (
          <div className="p-6 text-center text-slate-400">Loading folders...</div>
        ) : collections.length === 0 ? (
          <div className="p-6 text-center text-slate-400">
            <p className="mb-4">No folders yet. Create your first folder to get started!</p>
          </div>
        ) : (
          <div className="p-6">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {collections.map((collection) => (
                <Link
                  key={collection.id}
                  to={`/folder/${collection.id}`}
                  className="border border-[#26313e] bg-[#111923] rounded p-4 hover:border-blue-500/60 transition-colors"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <h3 className="font-semibold text-slate-100">{collection.name}</h3>
                      <p className="text-sm text-slate-400 mt-1">{collection.query}</p>
                    </div>
                    <span className="px-2 py-1 text-xs font-medium bg-blue-100 text-blue-800 rounded">
                      {collection.type}
                    </span>
                  </div>

                  <div className="mt-4 flex items-center justify-between text-sm">
                    <span className="text-slate-400">{collection.image_count} images</span>
                    <div className="flex space-x-2">
                      {collection.sources.map((source) => (
                        <span
                          key={source.provider}
                          className={`text-xs ${
                            source.enabled ? 'text-green-600' : 'text-slate-400'
                          }`}
                        >
                          {source.provider}
                        </span>
                      ))}
                    </div>
                  </div>

                  {collection.last_sync_at && (
                    <div className="mt-2 text-xs text-slate-400">
                      Last synced: {new Date(collection.last_sync_at).toLocaleString()}
                    </div>
                  )}
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default Dashboard;
