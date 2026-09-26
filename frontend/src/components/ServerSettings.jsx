import React, { useEffect, useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import api from '../api/client';
export default function ServerSettings() {
  const query = useQuery({ queryKey: ['server-capabilities'], queryFn: async () => (await api.settings.getServer()).data, refetchInterval: 15000 });
  const [reserve, setReserve] = useState('');
  useEffect(() => { if (query.data) setReserve(String(query.data.reserve_gib)); }, [query.data?.reserve_gib]);
  const save = useMutation({ mutationFn: () => api.settings.updateStorage({ reserve_gib: Number(reserve) }), onSuccess: () => query.refetch(), meta: { successMessage: 'Storage reserve saved.' } });
  const data = query.data;
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
  </section>;
}
