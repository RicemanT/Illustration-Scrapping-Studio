import React, { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
const inputClass = 'block mt-1 w-full rounded border border-slate-700 bg-[#090d12] px-3 py-2';
export default function ProcessingSettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['processing-settings'], queryFn: async () => (await api.settings.getProcessing()).data });
  const [form, setForm] = useState(null);
  useEffect(() => { if (query.data) setForm(query.data); }, [query.data]);
  const save = useMutation({ mutationFn: () => api.settings.updateProcessing(form), onSuccess: ({ data }) => client.setQueryData(['processing-settings'], data), meta: { successMessage: 'Processing settings saved for future jobs.' } });
  if (!form) return <section className="rounded border border-slate-800 bg-[#0c1219] p-4 space-y-3">
    <h2 className="font-semibold">Import processing</h2>
    {query.isError ? <div role="alert" className="space-y-2 text-sm">
      <p>{query.error?.response?.status === 404
        ? 'This backend does not provide processing settings. If you just updated the app, stop and restart the backend, then retry.'
        : !query.error?.response
          ? 'Could not reach the backend. Check that it is running, then retry.'
          : `Processing settings could not be loaded (HTTP ${query.error.response.status}). Check Logs for the failure details, then retry.`}</p>
      <button type="button" className="rounded bg-[#344a73] px-3 py-2 text-xs" disabled={query.isFetching} onClick={() => query.refetch()}>{query.isFetching ? 'Retrying...' : 'Retry processing settings'}</button>
    </div> : <p>Loading processing settings...</p>}
  </section>;
  const change = (key, value) => setForm(old => ({ ...old, [key]: value }));
  const numeric = (key, label, min, max) => <label className="text-xs">{label}<input aria-label={label} className={inputClass} type="number" min={min} max={max} required value={form[key]} onChange={e => change(key, Number(e.target.value))} /></label>;
  return <section className="rounded border border-slate-800 bg-[#0c1219] p-4 space-y-3">
    <h2 className="font-semibold">Import processing</h2>
    <p className="text-xs text-slate-400">Applies to future jobs. Existing images are untouched; already imported posts remain deduplicated. Active jobs keep their processing settings.</p>
    <form className="space-y-3" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={form.enabled} onChange={e => change('enabled', e.target.checked)} /> Enable resizing and re-encoding</label>
      {!form.enabled && <p className="text-xs text-blue-200">Still images retain their original bytes, resolution and format. Video, animations and archives still need frame extraction; extracted stills receive no additional resizing or re-encoding. Collection quality filters still apply.</p>}
      <fieldset disabled={!form.enabled} className="grid sm:grid-cols-2 gap-3 disabled:opacity-40">
        <label className="text-xs">Maximum longest side (pixels)<input aria-label="Maximum longest side" className={inputClass} type="number" min="1" value={form.max_dimension ?? ''} placeholder="No resizing limit" onChange={e => change('max_dimension', e.target.value === '' ? null : Number(e.target.value))} /><span className="text-slate-400">Blank means no resizing. Smaller images are never upscaled.</span></label>
        <label className="text-xs">Resize filter<select className={inputClass} value={form.resize_filter} onChange={e => change('resize_filter', e.target.value)}>{['area','lanczos','cubic','linear'].map(value => <option key={value}>{value}</option>)}</select></label>
        <label className="text-xs">Output format<select className={inputClass} value={form.output_format} onChange={e => change('output_format', e.target.value)}><option value="auto">JPEG stays JPEG; others become WebP</option><option value="webp">WebP</option><option value="jpg">JPEG</option><option value="png">PNG</option></select></label>
        <label className="text-xs flex items-center gap-2"><input type="checkbox" checked={form.webp_lossless} onChange={e => change('webp_lossless', e.target.checked)} /> Lossless WebP</label>
        {numeric('webp_quality', 'WebP quality / lossless effort', 0, 100)}
        {numeric('webp_method', 'WebP encoding method (0 fast, 6 slow)', 0, 6)}
        {numeric('jpeg_quality', 'JPEG quality', 1, 100)}
      </fieldset>
      <button className="rounded bg-[#344a73] px-3 py-2 text-xs" disabled={save.isPending}>Save processing settings</button>
      {save.isError && <p className="text-red-300 text-xs">Could not save. Check the numeric ranges and retry.</p>}
    </form>
  </section>;
}
