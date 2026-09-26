import React, { useState, useEffect } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import api from '../api/client';
import ImageDetail from './ImageDetail';
import { control } from './LocalFilters';

const errorText = (error) => error?.response?.data?.detail || error?.message || 'Unknown error';

function DatasetTools({ collectionId, filters = {}, onShowImages }) {
  const [jobId, setJobId] = useState(null);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [detail, setDetail] = useState(null);
  const jobs = useQuery({ queryKey: ['dataset-jobs', collectionId], queryFn: async () => (await api.dataset.history(collectionId)).data.items, refetchInterval: 2000 });
  useEffect(() => { if (!jobId && jobs.data?.length) setJobId(jobs.data[0].job_id); }, [jobId, jobs.data]);
  const jobQuery = useQuery({ queryKey: ['dataset-job', jobId], queryFn: async () => (await api.dataset.job(jobId)).data, enabled: Boolean(jobId), refetchInterval: q => ['queued','running','cancelling'].includes(q.state.data?.status) ? 1000 : false });
  const job = jobQuery.data;
  const active = ['queued','running','cancelling'].includes(job?.status);
  const issueQuery = useQuery({ queryKey: ['dataset-issues', jobId, offset], queryFn: async () => (await api.dataset.issues(jobId, offset)).data, enabled: Boolean(jobId), refetchInterval: active ? 2000 : false });
  useEffect(() => { if (job?.status) issueQuery.refetch(); }, [job?.status]);
  const selectJob = value => { setJobId(value); setOffset(0); setSelected(new Set()); };
  const validation = job?.kind === 'scan' && job.status === 'completed' ? { image_count: job.progress.total, valid_image_count: null, ready: job.result?.ready, summary: job.progress } : null;
  const repairMutation = useMutation({ mutationFn: () => api.dataset.repair(collectionId, jobId, [...selected]), onSuccess: r => { selectJob(r.data.job_id); jobs.refetch(); } });
  const cancelMutation = useMutation({ mutationFn: () => api.dataset.cancel(jobId), onSuccess: () => jobQuery.refetch() });
  const resumeMutation = useMutation({ mutationFn: () => api.dataset.resume(jobId), onSuccess: () => jobQuery.refetch() });
  const [mode, setMode] = useState('copy');

  const history = useQuery({
    queryKey: ['dataset-exports', collectionId],
    queryFn: async () => (await api.exports.list(collectionId)).data.items,
  });
  const validateMutation = useMutation({
    mutationFn: async () => (await api.dataset.scan(collectionId, filters)).data,
    onSuccess: result => { selectJob(result.job_id); jobs.refetch(); },
  });
  const exportMutation = useMutation({
    mutationFn: async () => (await api.exports.create(collectionId, mode)).data,
    onSuccess: (result) => {
      history.refetch();

    },
  });

  const issues = issueQuery.data?.items || [];
  const latestExport = history.data?.[0];

  return (
    <div className="p-4 space-y-4 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-200">Folder dataset validation</h2>
          <p className="mt-1 text-xs text-slate-400">Checks canonical files, curated sidecars, hashes, decode integrity, formats, paths, and ground-truth tags. It never changes your library.</p>
        </div>
        <button type="button" onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending || active} className="px-3 py-1.5 rounded bg-[#273451] text-blue-200 disabled:opacity-50">
          {validateMutation.isPending ? 'Starting?' : 'Validate current filter scope'}
        </button>
      </div>

      {validateMutation.isError && <div className="text-xs text-red-400">Validation failed: {errorText(validateMutation.error)}</div>}
      <div className="flex flex-wrap gap-2 text-xs text-slate-400">
        <select aria-label="Dataset job history" className={control} value={jobId || ''} onChange={e => selectJob(e.target.value)}><option value="">Job history</option>{jobs.data?.map(j => <option key={j.job_id} value={j.job_id}>{j.kind} ? {j.status} ? {j.created_at}</option>)}</select>
        {job && <span>{job.kind}: {job.status} ? {job.progress.completed}/{job.progress.total} images ? {job.progress.errors} errors ? {job.progress.warnings} warnings</span>}
        {active && <button className={control} disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate()}>Cancel job</button>}
        {['interrupted','canceled','failed'].includes(job?.status) && <button className={control} disabled={resumeMutation.isPending} onClick={() => resumeMutation.mutate()}>Resume remaining images</button>}
      </div>
      {job?.error && <p className="text-xs text-red-400">{job.error}</p>}
      {[repairMutation,cancelMutation,resumeMutation].filter(m => m.isError).map((m,i) => <p key={i} className="text-xs text-red-400">{String(errorText(m.error))}</p>)}
      {job && <div className="space-y-2">
        <div className="flex gap-2 text-xs"><button className={control} disabled={!issues.some(i => i.image_id)} onClick={() => onShowImages([...new Set(issues.map(i => i.image_id).filter(Boolean))])}>View this issue page in Gallery</button><button className={control} disabled={!selected.size || job.status !== 'completed' || job.kind !== 'scan' || repairMutation.isPending} onClick={() => { if (confirm(`Repair ${selected.size} explicitly selected issues? Only missing/broken thumbnails and missing/stale sidecars are rebuilt from stored images and current curated tags.`)) repairMutation.mutate(); }}>Repair {selected.size} selected issues</button></div>
        <div className="max-h-80 overflow-auto divide-y divide-[#202a34] border border-[#202a34] rounded">{issues.map(issue => <div className="p-2 text-xs flex gap-2" key={issue.issue_id}>
          {job.kind === 'scan' && issue.allowed_actions?.length > 0 && <input aria-label={`Select repair for image ${issue.image_id} ${issue.code}`} type="checkbox" checked={selected.has(issue.issue_id)} onChange={() => setSelected(current => { const next = new Set(current); next.has(issue.issue_id) ? next.delete(issue.issue_id) : next.add(issue.issue_id); return next; })} />}
          <span className={issue.severity === 'error' ? 'text-red-400' : 'text-amber-400'}>{issue.severity}</span>
          {issue.image_id && <button className="text-blue-300 shrink-0" onClick={() => setDetail({ id: issue.image_id })}>Image {issue.image_id}</button>}
          <div className="min-w-0 text-slate-300"><strong>{issue.code}</strong> ? {issue.message || issue.status}<div className="break-all text-slate-400">{issue.path}</div>{issue.before_sha256 !== undefined && <div className="break-all text-slate-400">Before: {issue.before_sha256 || 'missing'} ? After: {issue.after_sha256}</div>}{issue.code === 'unresolved_duplicate' && <span>Open Duplicates to compare these images manually.</span>}</div>
        </div>)}</div>
        <div className="flex gap-2 text-xs text-slate-400"><button className={control} disabled={!offset} onClick={() => { setOffset(Math.max(0,offset-100)); setSelected(new Set()); }}>Previous issues</button><span>{issueQuery.data?.total || 0} issues / audit results</span><button className={control} disabled={!issueQuery.data?.next_cursor} onClick={() => { setOffset(Number(issueQuery.data.next_cursor)); setSelected(new Set()); }}>Next issues</button></div>
      </div>}
      {detail && <ImageDetail image={detail} onClose={() => setDetail(null)} />}
      {validation && <>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <Metric label="Images" value={validation.image_count} />

          <Metric label="Errors" value={validation.summary.errors} tone={validation.summary.errors ? 'text-red-300' : 'text-emerald-300'} />
          <Metric label="Warnings" value={validation.summary.warnings} tone={validation.summary.warnings ? 'text-amber-300' : 'text-emerald-300'} />
        </div>
        <div className={`rounded border p-3 text-xs ${validation.ready ? 'border-emerald-800 bg-emerald-950/30 text-emerald-300' : 'border-red-900 bg-red-950/20 text-red-300'}`}>
          {validation.ready ? 'This scanned scope passed. Export validates the entire folder again.' : 'Resolve the errors below before exporting.'}
        </div>
        <div className="border border-[#202a34] rounded p-3 space-y-2">
          <h3 className="font-medium text-slate-200">Create versioned export</h3>
          <p className="text-xs text-slate-400">Exports the canonical image and its existing curated .txt sidecar under library/exports. A JSON manifest records hashes, review state, original-media provenance, and every provider metadata snapshot. No captions are generated.</p>
          <div className="flex flex-wrap items-center gap-2">
            <select value={mode} onChange={(event) => setMode(event.target.value)} className="px-2 py-1.5 bg-[#090d12] border border-[#202a34] rounded text-xs">
              <option value="copy">Copy files</option>
              <option value="hardlink">Hardlink (copy fallback)</option>
            </select>
            <button type="button" onClick={() => exportMutation.mutate()} disabled={!validation.ready || exportMutation.isPending} className="px-3 py-1.5 rounded bg-emerald-800 text-emerald-100 disabled:opacity-40">
              {exportMutation.isPending ? 'Exporting...' : 'Create export'}
            </button>
          </div>
          {exportMutation.isError && <div className="text-xs text-red-400">Export failed: {errorText(exportMutation.error)}</div>}
          {latestExport?.status === 'completed' && <div className="text-xs text-emerald-300 break-all">Latest export: {latestExport.output_path}</div>}
          {latestExport?.status === 'failed' && <div className="text-xs text-red-400">Latest export failed: {latestExport.error}</div>}
        </div>
      </>}
    </div>
  );
}

function Metric({ label, value, tone = 'text-slate-200' }) {
  return <div className="border border-[#202a34] rounded p-2"><div className="text-[11px] uppercase tracking-wide text-slate-400">{label}</div><div className={`mt-1 text-lg font-semibold ${tone}`}>{value}</div></div>;
}

export default DatasetTools;
