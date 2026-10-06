import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const field = 'rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm';
const button = 'rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-40';
const quiet = 'rounded border border-slate-700 px-3 py-2 text-sm disabled:opacity-40';
const number = (value) => Number(value || 0).toLocaleString();
const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg?.replace(/^Value error, /, '') || String(item)).join('; ');
  return typeof detail === 'string' ? detail : String(error?.message || 'Request failed');
};
const duration = (seconds) => {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
};
const MODELS = [
  ['dinov2', 'DINOv2-L (style)'], ['dinov3', 'DINOv3-L (style; needs a Hugging Face token)'], ['ws3', 'waifu-scorer v3'], ['ws4', 'waifu-scorer v4-beta'],
  ['naflex', 'Naflex (SigLIP2)'], ['aps25', 'aesthetic-predictor v2.5'], ['deepghs', 'DeepGHS: dbaesthetic, sketch/comic/3D/photo/AI/monochrome, style era'],
  ['anzhc', "Anzhc's score (Danbooru score band)"],
];
const bytes = (value) => (value >= 2 ** 30 ? `${(value / 2 ** 30).toFixed(1)} GB` : `${Math.round((value || 0) / 2 ** 20)} MB`);
const ACTIVE = ['queued', 'running', 'stopping'];
const auc = (value) => (value == null ? '—' : value.toFixed(3));

function GpuTable({ gpus, allowed }) {
  if (!gpus?.length) return <p className="text-xs text-slate-500">No NVIDIA GPU visible to the backend (nvidia-smi not found).</p>;
  return (
    <table className="text-xs">
      <thead><tr className="text-left text-slate-400"><th className="pr-4">GPU</th><th className="pr-4">Used by analysis</th><th className="pr-4">Busy</th><th className="pr-4">Temperature</th><th className="pr-4">Memory</th><th>Power</th></tr></thead>
      <tbody>{gpus.map((gpu) => (
        <tr key={gpu.index}>
          <td className="pr-4 text-slate-300">{gpu.index} · {gpu.name}</td>
          <td className="pr-4">{allowed.includes(gpu.index) ? <span className="text-blue-200">yes</span> : <span className="text-slate-500">no</span>}</td>
          <td className="pr-4 tabular-nums">{gpu.utilization ?? '—'}%</td>
          <td className={`pr-4 tabular-nums ${gpu.temperature >= 83 ? 'text-red-300' : gpu.temperature >= 75 ? 'text-amber-300' : 'text-slate-300'}`}>{gpu.temperature ?? '—'} °C</td>
          <td className="pr-4 tabular-nums">{number(gpu.memory_used_mb)} / {number(gpu.memory_total_mb)} MB</td>
          <td className="tabular-nums">{gpu.power_w != null ? `${Math.round(gpu.power_w)} W` : '—'}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

// Planner section: GPU image analysis (style, aesthetics, content) for every candidate post.
export default function AnalysisPanel() {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ['analysis-status'],
    queryFn: async () => (await api.analysis.status()).data,
    refetchInterval: (query) => {
      const data = query.state.data;
      return ACTIVE.includes(data?.job?.status) || data?.install?.status === 'running' || data?.selftest?.status === 'running' ? 3000 : 20000;
    },
  });
  const data = status.data;
  const [draft, setDraft] = useState(null);
  const config = draft || data?.config;
  const set = (key, value) => setDraft({ ...config, [key]: value });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['analysis-status'] });
  const save = useMutation({ mutationFn: () => api.analysis.saveConfig(config), onSuccess: () => { setDraft(null); refresh(); } });
  const [token, setToken] = useState('');
  const saveToken = useMutation({ mutationFn: () => api.analysis.saveToken(token), onSuccess: () => { setToken(''); refresh(); } });
  const install = useMutation({ mutationFn: api.analysis.install, onSuccess: refresh });
  const clearSamples = useMutation({ mutationFn: api.analysis.clearSamples, onSuccess: refresh });
  const selftest = useMutation({ mutationFn: api.analysis.selftest, onSuccess: refresh });
  const [scope, setScope] = useState('planner');
  const [reanalyze, setReanalyze] = useState(false);
  const start = useMutation({ mutationFn: () => api.analysis.start({ scope, reanalyze }), onSuccess: refresh });
  const stop = useMutation({ mutationFn: api.analysis.stop, onSuccess: refresh });
  const resume = useMutation({ mutationFn: (jobId) => api.analysis.resume(jobId), onSuccess: refresh });
  const calibration = useMutation({ mutationFn: async (source) => (await api.analysis.calibration(source)).data });
  const reset = useMutation({ mutationFn: async () => (await api.analysis.resetCuration()).data,
    onSuccess: () => { for (const key of [['planner-folder'], ['collection-images'], ['tracker-folders'], ['tracker-folder']]) queryClient.invalidateQueries({ queryKey: key }); } });
  const gpus = useQuery({ queryKey: ['analysis-gpus'], queryFn: async () => (await api.analysis.gpus()).data, refetchInterval: 5000, enabled: Boolean(data) });
  useEffect(() => { if (install.data || data?.install?.status === 'completed') queryClient.invalidateQueries({ queryKey: ['analysis-status'] }); }, [data?.install?.status]);

  const env = data?.environment;
  const job = data?.job;
  const running = ACTIVE.includes(job?.status);
  const progress = job?.progress || {};
  const total = progress.total || 0;
  const finished = (progress.done || 0) + (progress.failed || 0);
  const allowed = (config?.gpus || '').split(',').map((part) => Number(part.trim())).filter((n) => Number.isInteger(n));
  const ranges = config?.dino_ranges || [];

  return (
    <section className="rounded border border-slate-800 p-4 space-y-4">
      <div>
        <h2 className="font-semibold">7. Image analysis</h2>
        <p className="text-xs text-slate-400">
          Looks at each candidate image on the server's GPU: DINOv2 and DINOv3 find each artist's style (the latest one; their main career style
          when the latest era has too few images), waifu-scorer v3/v4, Naflex, aesthetic-predictor v2.5 and DeepGHS dbaesthetic score aesthetics, and
          DeepGHS classifiers spot sketches, monochrome, comic pages, 3D, photos and AI images (excluded unless they are most of the artist's style).
          Only a medium-size sample of each post is downloaded and nothing is kept but the scores. Then tick <b>Use image analysis</b> in 3. Plan.
        </p>
      </div>

      {!data && <p className="text-sm text-slate-400">{status.isError ? errorText(status.error) : 'Checking the analysis environment…'}</p>}
      {data && <>
        <div className="space-y-2 rounded border border-slate-800 p-3">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className={env?.ready ? 'text-green-300' : 'text-amber-300'}>{env?.ready ? 'Analysis packages installed' : 'Analysis packages missing'}</span>
            {env && <span className="text-xs text-slate-400">torch {env.torch || '—'}{env.torch_cuda ? ` (CUDA ${env.torch_cuda})` : ''}{env.driver ? ` · driver ${env.driver} (CUDA ≤ ${env.driver_cuda || '?'})` : ''} · transformers {env.transformers || '—'} · onnxruntime {env.onnxruntime || '—'}
              {env.cuda ? ` · CUDA, ${env.devices?.length || 0} GPU${env.devices?.length === 1 ? '' : 's'}` : ' · no CUDA'}</span>}
            <button className={quiet} disabled={data.install?.status === 'running' || install.isPending} onClick={() => install.mutate()}>
              {data.install?.status === 'running' ? 'Installing…' : env?.cuda_problem || env?.onnx_problem ? 'Install packages for this GPU driver' : env?.ready ? 'Reinstall / update packages' : 'Install analysis packages'}</button>
            <button className={quiet} onClick={() => queryClient.fetchQuery({ queryKey: ['analysis-status'], queryFn: async () => (await api.analysis.status(true)).data })}>Check again</button>
          </div>
          {env?.cuda_problem && <p className="text-sm text-amber-300">{env.cuda_problem} Analysis would run on the CPU, so jobs refuse to start.
            {env.torch_index ? ` The button above installs the PyTorch build from ${env.torch_index} (no app restart needed); then Check again and run the model test.` : ''}</p>}
          {env?.onnx_problem && <p className="text-sm text-amber-300">{env.onnx_problem} The button above installs the matching build (no app restart needed); then Check again and run the model test.</p>}
          {(data.install?.status === 'running' || data.install?.status === 'failed') && <pre className="max-h-40 overflow-auto rounded bg-black/40 p-2 text-[11px] text-slate-400">{data.install.log}</pre>}
          {install.isError && <p className="text-sm text-red-400">{errorText(install.error)}</p>}
          <GpuTable gpus={gpus.data?.gpus || data.gpus} allowed={allowed} />
        </div>

        {config && <div className="space-y-3 rounded border border-slate-800 p-3">
          <h3 className="text-sm font-semibold text-slate-200">Settings</h3>
          <div className="flex flex-wrap items-end gap-3 text-sm">
            <label className="text-xs text-slate-400" title="CUDA device numbers, e.g. 0,1. Leave empty to run on the CPU (slow).">GPUs to use
              <input className={`${field} block w-28`} value={config.gpus} onChange={(e) => set('gpus', e.target.value)} /></label>
            <label className="text-xs text-slate-400" title="The worker pauses after each batch so the GPUs are busy at most this share of the time">Max GPU busy %
              <input type="number" min="5" max="100" className={`${field} block w-24`} value={Math.round(config.duty * 100)} onChange={(e) => set('duty', Math.min(1, Math.max(0.05, Number(e.target.value) / 100)))} /></label>
            <label className="text-xs text-slate-400">Batch size
              <input type="number" min="1" max="256" className={`${field} block w-24`} value={config.batch_size} onChange={(e) => set('batch_size', Number(e.target.value))} /></label>
            <label className="text-xs text-slate-400" title="Each artist's newest posts; the latest style is decided here">Newest posts per artist
              <input type="number" min="0" className={`${field} block w-28`} value={config.newest_posts} onChange={(e) => set('newest_posts', Number(e.target.value))} /></label>
            <label className="text-xs text-slate-400" title="Spread evenly over the rest of the career, for the main career style">Older posts sampled
              <input type="number" min="0" className={`${field} block w-28`} value={config.older_posts} onChange={(e) => set('older_posts', Number(e.target.value))} /></label>
            <label className="text-xs text-slate-400" title="Seconds between sample downloads per site; Danbooru, e621 and Gelbooru download side by side">Download pace (s)
              <input type="number" min="0.05" step="0.05" className={`${field} block w-24`} value={config.download_interval} onChange={(e) => set('download_interval', Number(e.target.value))} /></label>
            <label className="text-xs text-slate-400" title="The worker pauses while a GPU it uses is this hot, until it is 5 °C cooler. 0 = never.">Pause at °C
              <input type="number" min="0" max="100" className={`${field} block w-20`} value={config.max_temp ?? 80} onChange={(e) => set('max_temp', Number(e.target.value))} /></label>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
            {MODELS.map(([key, label]) => <label key={key} className="flex items-center gap-2"><input type="checkbox" checked={Boolean(config[key])} onChange={(e) => set(key, e.target.checked)} />{label}</label>)}
          </div>
          <label className="flex items-center gap-2 text-sm" title="Samples stay in the planner folder (analysis-samples), so a model ticked later is run without downloading again. Roughly 150 KB per post.">
            <input type="checkbox" checked={config.keep_samples !== false} onChange={(e) => set('keep_samples', e.target.checked)} />Keep downloaded samples for later runs</label>
          <p className="text-xs text-slate-500">Ticking a model later and pressing <b>Start analysis</b> analyses only the posts that model has not seen; earlier scores are kept.</p>
          <div className="flex flex-wrap items-end gap-3 text-sm">
            <label className="text-xs text-slate-400" title="Transformer blocks whose features are stored, e.g. 4-6, 10-14, 20-24. Calibration compares them.">DINO block ranges stored
              <input className={`${field} block w-48`} value={ranges.join(', ')} onChange={(e) => set('dino_ranges', e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} /></label>
            <label className="text-xs text-slate-400">Range used for style
              <select className={`${field} block`} value={config.style_range} onChange={(e) => set('style_range', e.target.value)}>
                {ranges.map((range) => <option key={range} value={range}>blocks {range}</option>)}</select></label>
            {draft && <button className={button} disabled={save.isPending} onClick={() => save.mutate()}>Save settings</button>}
            {draft && <button className={quiet} onClick={() => setDraft(null)}>Discard</button>}
          </div>
          {save.isError && <p className="text-sm text-red-400">{errorText(save.error)}</p>}
          <form className="flex flex-wrap items-end gap-3 text-sm" onSubmit={(e) => { e.preventDefault(); saveToken.mutate(); }}>
            <label className="text-xs text-slate-400">Hugging Face token (read access)
              <input type="password" autoComplete="off" className={`${field} block w-72`} value={token} placeholder={data.hf_token_set ? 'Saved — enter a new one to replace it' : 'hf_…'}
                onChange={(e) => setToken(e.target.value)} /></label>
            <button className={quiet} disabled={saveToken.isPending}>{token ? 'Save token' : data.hf_token_set ? 'Remove token' : 'Save token'}</button>
            <span className="text-xs text-slate-400">DINOv3 is gated: accept its licence on <a className="underline" href="https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m" target="_blank" rel="noreferrer">its Hugging Face page</a> with the same account. Stored on the server only.</span>
          </form>
        </div>}

        <div className="space-y-2 rounded border border-slate-800 p-3">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-semibold text-slate-200">Test the models</h3>
            <button className={quiet} disabled={!env?.ready || data.selftest?.status === 'running' || selftest.isPending} onClick={() => selftest.mutate()}>
              {data.selftest?.status === 'running' ? 'Testing… (first run downloads ~6 GB of models)' : 'Run model test'}</button>
            <span className="text-xs text-slate-400">Loads every enabled model on the chosen GPUs and scores a test image.</span>
          </div>
          {selftest.isError && <p className="text-sm text-red-400">{errorText(selftest.error)}</p>}
          {data.selftest?.results?.length > 0 && <table className="text-xs">
            <tbody>{data.selftest.results.filter((r) => r.model || r.load || r.result).map((r, index) => (
              <tr key={index} className="align-top">
                <td className="pr-4 text-slate-300">{r.model || (r.load ? 'loading' : 'result')}</td>
                <td className={`pr-4 ${r.ok === false ? 'text-red-300' : 'text-green-300'}`}>{r.ok === false ? r.error : r.load ? Object.entries(r.load).map(([k, v]) => `${k}: ${v}`).join(' · ') : r.result || 'ok'}</td>
                <td className="text-slate-500">{r.detail || ''}{r.seconds != null ? `${r.seconds} s for ${r.images} · ` : ''}{r.scores ? Object.entries(r.scores).map(([k, v]) => `${k} ${Number(v).toFixed(2)}${r.spread?.[k] ? ` (${r.spread[k].map((x) => Number(x).toFixed(1)).join('–')})` : ''}`).join(' · ') : ''}{r.vectors ? ` · vectors ${Object.keys(r.vectors).join(', ')}` : ''}</td>
              </tr>))}</tbody></table>}
          {data.selftest?.status === 'failed' && <pre className="max-h-40 overflow-auto rounded bg-black/40 p-2 text-[11px] text-slate-400">{data.selftest.log}</pre>}
        </div>

        <div className="space-y-2 rounded border border-slate-800 p-3">
          <h3 className="text-sm font-semibold text-slate-200">Analyse</h3>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <select className={field} value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="planner">Artists with planner collections</option>
              <option value="enabled">Every artist enabled for planning</option>
            </select>
            <label className="flex items-center gap-2 text-xs text-slate-300" title="Run again for posts that were already analysed (after changing models or block ranges)"><input type="checkbox" checked={reanalyze} onChange={(e) => setReanalyze(e.target.checked)} /> Analyse again</label>
            <button className={button} disabled={!env?.ready || running || start.isPending || Boolean(draft)} onClick={() => start.mutate()}>{start.isPending ? 'Queuing…' : 'Start analysis'}</button>
            {running && <button className={quiet} disabled={stop.isPending || job.status === 'stopping'} onClick={() => stop.mutate()}>{job.status === 'stopping' ? 'Stopping…' : 'Stop'}</button>}
            {!running && job && ['stopped', 'interrupted', 'failed'].includes(job.status) && <button className={quiet} disabled={resume.isPending} onClick={() => resume.mutate(job.id)}>Resume job #{job.id}</button>}
            {draft && <span className="text-xs text-amber-300">Save the settings first.</span>}
          </div>
          {[start.error, stop.error, resume.error].filter(Boolean).map((error, index) => <p key={index} className="text-sm text-red-400">{errorText(error)}</p>)}
          {job && <div className="space-y-1 text-xs text-slate-300">
            <div>Job #{job.id} · {job.status}{progress.phase ? ` · ${progress.phase}` : ''} · {number(finished)} of {number(total)} posts
              {progress.failed ? ` (${number(progress.failed)} could not be downloaded)` : ''}{progress.rate ? ` · ${progress.rate} per second · about ${duration(progress.eta_seconds)} left` : ''}
              {progress.gpu_busy != null ? ` · ${progress.gpus > 1 ? `${progress.gpus} GPUs, each` : 'GPU'} busy ${Math.round(progress.gpu_busy * 100)}% of the time` : ''}</div>
            {total > 0 && <div className="h-1.5 overflow-hidden rounded bg-[#0c1219]"><div className="h-full bg-blue-500" style={{ width: `${Math.round((100 * finished) / total)}%` }} /></div>}
            {progress.models && <div className="flex flex-wrap gap-x-3 text-slate-400">{Object.entries(progress.models).map(([name, state]) => (
              <span key={name} className={String(state).startsWith('ok') ? '' : 'text-red-300'}>{name}: {String(state)}</span>))}</div>}
            {job.error && <p className="text-red-300">{job.error}</p>}
            {(job.status === 'failed' || job.status === 'interrupted') && job.log && <pre className="max-h-40 overflow-auto rounded bg-black/40 p-2 text-[11px] text-slate-400">{job.log}</pre>}
          </div>}
          {data.stats && <p className="text-xs text-slate-500">Analysed so far: {number(data.stats.posts?.done)} posts{data.stats.posts?.failed ? `, ${number(data.stats.posts.failed)} failed` : ''}
            {Object.keys(data.stats.vectors || {}).length > 0 && ` · style vectors: ${Object.entries(data.stats.vectors).map(([k, v]) => `${k} ${number(v)}`).join(', ')}`}
            {data.stats.samples?.count > 0 && <> · kept samples: {number(data.stats.samples.count)} ({bytes(data.stats.samples.bytes)}){' '}
              <button className="text-slate-400 underline disabled:opacity-40" disabled={clearSamples.isPending || running}
                onClick={() => { if (window.confirm('Delete every kept sample? Later runs will download them again.')) clearSamples.mutate(); }}>delete</button></>}</p>}
          {clearSamples.isError && <p className="text-sm text-red-400">{errorText(clearSamples.error)}</p>}
          {Object.keys(data.stats?.score_summary || {}).length > 0 && <p className="text-xs text-slate-500">Scores so far (mean, range): {Object.entries(data.stats.score_summary).map(([name, s]) => (
            <span key={name} className={`mr-3 ${s.count >= 20 && s.max - s.min < 0.01 ? 'text-red-300' : ''}`} title={s.count >= 20 && s.max - s.min < 0.01 ? 'The same value on every image: this scorer is not working' : `${number(s.count)} images`}>
              {name} {s.mean.toFixed(2)} ({s.min.toFixed(1)}–{s.max.toFixed(1)})</span>))}</p>}
        </div>

        <div className="space-y-2 rounded border border-slate-800 p-3">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-sm font-semibold text-slate-200">Calibrate against your curation</h3>
            <button className={quiet} disabled={calibration.isPending} onClick={() => calibration.mutate('auto')}>{calibration.isPending ? 'Comparing…' : 'Compare'}</button>
          </div>
          <p className="text-xs text-slate-400">Measures how well each style block range and scorer agrees with what you removed (and marked) by hand, so the best ones drive the plan. AUC 0.5 is chance, 1.0 is perfect agreement.</p>
          {calibration.isError && <p className="text-sm text-red-400">{errorText(calibration.error)}</p>}
          {calibration.data && <div className="space-y-2 text-xs">
            <p className="text-slate-400">Using your {calibration.data.source === 'backup' ? 'decisions saved by the reset' : 'current collections'}: {number(calibration.data.artists)} artists, {number(calibration.data.removed)} removed and {number(calibration.data.kept)} kept images.</p>
            <table><thead><tr className="text-left text-slate-400"><th className="pr-4">Style model · blocks</th><th className="pr-4">AUC</th><th className="pr-4">Removed / kept</th><th /></tr></thead>
              <tbody>{calibration.data.style.map((row, index) => (
                <tr key={row.model}><td className="pr-4 text-slate-300">{row.model}</td><td className={`pr-4 tabular-nums ${index === 0 ? 'text-green-300' : ''}`}>{auc(row.auc)}</td>
                  <td className="pr-4 tabular-nums text-slate-400">{row.removed} / {row.kept}</td>
                  <td>{config && row.model.includes(':') && config.style_range === row.model.split(':')[1] && <span className="text-slate-400">in use</span>}
                    {config && row.model.includes(':') && ranges.includes(row.model.split(':')[1]) && config.style_range !== row.model.split(':')[1] &&
                    <button className="text-blue-300 underline" onClick={() => set('style_range', row.model.split(':')[1])}>use blocks {row.model.split(':')[1]}</button>}</td></tr>))}</tbody></table>
            <table><thead><tr className="text-left text-slate-400"><th className="pr-4">Scorer</th><th className="pr-4">Kept vs removed</th><th className="pr-4">Your quality marks</th><th>Your aesthetic marks</th></tr></thead>
              <tbody>{calibration.data.scorers.map((row) => (
                <tr key={row.scorer}><td className="pr-4 text-slate-300">{row.label}</td><td className="pr-4 tabular-nums">{auc(row.kept_vs_removed)}</td>
                  <td className="pr-4 tabular-nums">{auc(row.quality_marks)}</td><td className="tabular-nums">{auc(row.aesthetic_marks)}</td></tr>))}</tbody></table>
            {calibration.data.anzhc_bands?.length > 0 && <table><thead><tr className="text-left text-slate-400"><th className="pr-4" title="Most likely band of Danbooru scores (top 10% … bottom 10%)">Anzhc band</th><th className="pr-4">Kept</th><th className="pr-4">Removed</th><th>You removed</th></tr></thead>
              <tbody>{calibration.data.anzhc_bands.map((row) => (
                <tr key={row.band}><td className="pr-4 text-slate-300">top {row.band}%</td><td className="pr-4 tabular-nums">{number(row.kept)}</td><td className="pr-4 tabular-nums">{number(row.removed)}</td>
                  <td className="tabular-nums">{Math.round(100 * row.removed / Math.max(1, row.kept + row.removed))}%</td></tr>))}</tbody></table>}
          </div>}
        </div>

        <div className="space-y-2 rounded border border-amber-900/60 p-3">
          <h3 className="text-sm font-semibold text-amber-200">Redo curation with the analysis</h3>
          <ol className="list-decimal space-y-1 pl-5 text-xs text-slate-400">
            <li>Analyse the artists with planner collections (above) and compare against your curation.</li>
            <li><b>Reset hand curation</b>: saves every decision (removals, locks, bans, accepted folders, marks, eras) to a backup file, then clears them. Images you removed become choosable again; the backup keeps them for calibration.</li>
            <li>In 3. Plan tick <b>Use image analysis</b> and run the plan; download it into the same group prefix (folders are reused).</li>
            <li>Use <b>Remove images no longer selected</b> in 5., then <b>Apply</b> quality and aesthetic tags in 6.</li>
            <li>Review each folder: flagged images come first.</li>
          </ol>
          <button className="rounded border border-amber-700 px-3 py-2 text-sm text-amber-100 hover:bg-amber-950/50 disabled:opacity-40" disabled={reset.isPending || running}
            onClick={() => { if (window.confirm('Back up and clear all hand curation of planner collections (locks, bans, accepted folders, quality/aesthetic marks, eras)? Images stay in the folders until you prune.')) reset.mutate(); }}>
            {reset.isPending ? 'Resetting…' : 'Reset hand curation'}</button>
          {reset.isError && <p className="text-sm text-red-400">{errorText(reset.error)}</p>}
          {reset.data && <p className="text-sm text-green-300">Reset {number(reset.data.artists)} artists in {number(reset.data.folders)} folders: {number(reset.data.overrides_cleared)} locks/bans cleared, {number(reset.data.completed_cleared)} accepted folders reopened, {number(reset.data.removals_released)} removals released. Backup: {reset.data.backup}</p>}
        </div>
      </>}
    </section>
  );
}
