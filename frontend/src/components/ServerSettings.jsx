import React, { useEffect, useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import api from '../api/client';
export default function ServerSettings() {
  const query = useQuery({ queryKey: ['server-capabilities'], queryFn: async () => (await api.settings.getServer()).data, refetchInterval: 15000 });
  const [reserve, setReserve] = useState('');
  useEffect(() => { if (query.data) setReserve(String(query.data.reserve_gib)); }, [query.data?.reserve_gib]);
  const save = useMutation({ mutationFn: () => api.settings.updateStorage({ reserve_gib: Number(reserve) }), onSuccess: () => query.refetch(), meta: { successMessage: 'Storage reserve saved.' } });
  const data = query.data;
  const [stopped, setStopped] = useState(false);
  const shutdown = useMutation({ mutationFn: () => api.settings.shutdown(), onSuccess: () => setStopped(true) });
  const confirmShutdown = () => {
    if (confirm("Shut down the whole app now?\n\nRunning syncs, imports, harvests and downloads stop and can be resumed after the next start. This page stops working until the app is started again (the notebook's Start once cell, or Start Studio).")) shutdown.mutate();
  };
  if (stopped) return <div role="alertdialog" aria-modal="true" aria-label="App stopped" className="fixed inset-0 z-[60] flex items-center justify-center bg-[#05080c]/95 p-6">
    <div className="max-w-md space-y-3 rounded-lg border border-[#2a3644] bg-[#0c1219] p-6 text-center">
      <div className="mx-auto h-3 w-3 rounded-full bg-red-400 shadow-[0_0_12px_rgba(248,113,113,0.8)]" />
      <h2 className="text-lg font-semibold text-slate-100">The app is shutting down</h2>
      <p className="text-sm text-slate-300">It finishes what it is writing and then exits. You can close this tab.</p>
      <p className="text-xs text-slate-400">To use it again, run the notebook's <b>Start once</b> cell (on Windows, Start Studio), then reload this page.</p>
    </div>
  </div>;
  return <section className="rounded border border-slate-800 bg-[#0c1219] p-4 space-y-3">
    <h2 className="font-semibold">Active server and storage</h2>
    {!data ? <p>{query.isError ? 'Server status unavailable. Check the connection and restart an older backend.' : 'Loading server status...'}</p> : <>
      <p className="text-sm">{data.jupyter ? 'Jupyter server' : 'App server'} · {data.platform} · Python {data.python} · {data.available_cpus} available logical CPUs</p>
      <p className="text-xs break-all">Library: {data.library_path}</p>
      <p className="text-sm">{(data.disk_free_bytes / 1024 ** 3).toFixed(1)} GiB free / {(data.disk_total_bytes / 1024 ** 3).toFixed(1)} GiB filesystem capacity</p>
      <p className="text-xs text-slate-400">{data.storage_note} All scraping, processing, credentials and files belong to this backend.</p>
      <form onSubmit={event => { event.preventDefault(); save.mutate(); }} className="flex items-end gap-3">
        <label className="text-xs">Keep free (GiB)<input required aria-label="Storage reserve GiB" type="number" min="0" max="1048576" step="0.1" value={reserve} onChange={event => setReserve(event.target.value)} className="block mt-1 rounded border border-slate-700 bg-[#090d12] px-3 py-2" /></label>
        <button disabled={save.isPending} className="rounded bg-[#344a73] px-3 py-2 text-xs">Save storage reserve</button>
      </form>
      <p className="text-xs text-slate-400">Below this reserve, new downloads and image ingestion fail with an explanation. This is an admission check, not a hard disk quota; in-flight downloads may cross the reserve. Failed items require a new job after space is available.</p>
    </>}
    <div className="flex flex-wrap items-center gap-3 border-t border-slate-800 pt-3">
      <button type="button" onClick={confirmShutdown} disabled={shutdown.isPending}
        className="rounded border border-red-800 bg-red-950/60 px-3 py-2 text-xs font-semibold text-red-100 hover:bg-red-900/70 disabled:opacity-50">
        {shutdown.isPending ? 'Shutting down...' : 'Shut down app'}
      </button>
      <span className="text-xs text-slate-400">Stops the backend and this UI cleanly, like the notebook's Stop cell. Start it again from the notebook.</span>
      {shutdown.isError && <span className="text-xs text-red-300">Could not shut down: {shutdown.error.response?.data?.detail || shutdown.error.message}</span>}
    </div>
  </section>;
}
