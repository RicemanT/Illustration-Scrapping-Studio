import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
import { describeQualityJob, useQualityJob } from './qualityJob';

const field = 'rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm';
const button = 'rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-40';
const quiet = 'rounded border border-slate-700 px-3 py-2 text-sm disabled:opacity-40';
const number = (value) => Number(value || 0).toLocaleString();
const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg?.replace(/^Value error, /, '') || String(item)).join('; ');
  return typeof detail === 'string' ? detail : String(error?.message || 'Request failed');
};
const label = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function Cell({ cell, config }) {
  if (!cell) return <td className="px-2 py-1 text-slate-600">—</td>;
  const value = (v) => (v == null ? '—' : number(v));
  return (
    <td className={`px-2 py-1 tabular-nums ${cell.enough ? '' : 'opacity-50'}`}
      title={cell.enough ? `${number(cell.n)} posts` : `Only ${number(cell.n)} posts: images here use the year's (or the whole site's) cut-offs`}>
      {config.masterpiece_top > 0 && <span className="text-amber-200">≥{value(cell.masterpiece)}</span>}
      {config.best_quality_top > 0 && <span className="text-sky-200"> · ≥{value(cell.best_quality)}</span>}
      {config.low_quality_bottom > 0 && <span className="text-rose-200"> · ≤{value(cell.low_quality)}</span>}
      <span className="block text-[10px] text-slate-500">{number(cell.n)}</span>
    </td>
  );
}

// Planner section: score-percentile quality tags for every planner collection.
export default function QualityTagsPanel() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ['quality-status'], queryFn: async () => (await api.planner.quality()).data });
  const thresholds = useQuery({ queryKey: ['quality-thresholds'], queryFn: async () => (await api.planner.qualityThresholds()).data });
  const [draft, setDraft] = useState(null);
  const [rebuild, setRebuild] = useState(false);
  const { job, running, start } = useQualityJob();
  const config = draft || status.data?.config;
  const save = useMutation({
    mutationFn: () => api.planner.saveQualityConfig(config),
    onSuccess: () => {
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: ['quality-status'] });
      queryClient.invalidateQueries({ queryKey: ['quality-thresholds'] });
    },
  });
  const set = (key, value) => setDraft({ ...config, [key]: value });
  const stats = status.data?.stats;
  const table = thresholds.data;
  const savedConfig = table?.config || status.data?.config;

  return (
    <section className="rounded border border-slate-800 p-4 space-y-3">
      <h2 className="font-semibold">6. Quality tags from scores</h2>
      <p className="text-xs text-slate-400">
        Marks images in planner collections as masterpiece, best quality or low quality from their post's score on the site.
        Raw scores favor newer and more explicit posts, so each post is ranked only against harvested posts from the same site,
        year and rating; buckets with too few posts use the whole year, then the whole site. Marks you set by hand in the image
        viewer are never changed, accepted collections are skipped, and very aesthetic / aesthetic stay manual. Like hand marks,
        the tags enter the sidecars when you accept a collection.
      </p>
      {config && <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="text-xs text-slate-400">Rank by
          <select className={`${field} block`} value={config.metric} onChange={(event) => set('metric', event.target.value)}>
            <option value="score">Score (up minus down votes)</option>
            <option value="favorites">Favorites</option>
          </select>
        </label>
        <label className="text-xs text-slate-400">Masterpiece: top %
          <input type="number" min="0" max="100" step="0.5" className={`${field} block w-24`} value={config.masterpiece_top} onChange={(event) => set('masterpiece_top', Number(event.target.value))} />
        </label>
        <label className="text-xs text-slate-400">Best quality: top %
          <input type="number" min="0" max="100" step="0.5" className={`${field} block w-24`} value={config.best_quality_top} onChange={(event) => set('best_quality_top', Number(event.target.value))} />
        </label>
        <label className="text-xs text-slate-400" title="0 never tags low quality. A low score can also mean few people saw the post.">Low quality: bottom %
          <input type="number" min="0" max="100" step="0.5" className={`${field} block w-24`} value={config.low_quality_bottom} onChange={(event) => set('low_quality_bottom', Number(event.target.value))} />
        </label>
        <label className="text-xs text-slate-400" title="Year/rating buckets with fewer harvested posts use a wider bucket">Smallest bucket (posts)
          <input type="number" min="1" className={`${field} block w-28`} value={config.min_bucket} onChange={(event) => set('min_bucket', Number(event.target.value))} />
        </label>
        <label className="flex items-center gap-2 text-xs text-slate-300"><input type="checkbox" checked={config.by_year} onChange={(event) => set('by_year', event.target.checked)} /> Separate years</label>
        <label className="flex items-center gap-2 text-xs text-slate-300"><input type="checkbox" checked={config.by_rating} onChange={(event) => set('by_rating', event.target.checked)} /> Separate ratings</label>
        <label className="flex items-center gap-2 text-xs text-slate-300" title="Also tag accepted collections, e.g. after adding wildcards to them. Marks set by hand are still never replaced.">
          <input type="checkbox" checked={Boolean(config.include_accepted)} onChange={(event) => set('include_accepted', event.target.checked)} /> Include accepted collections</label>
        <label className="flex items-center gap-2 text-xs text-slate-300" title="From the image-analysis scorer ensemble; needs 7. Image analysis"><input type="checkbox" checked={config.aesthetic_tags ?? true} onChange={(event) => set('aesthetic_tags', event.target.checked)} /> Aesthetic tags from scorers</label>
        {config.aesthetic_tags !== false && <>
          <label className="text-xs text-slate-400">Very aesthetic: top %
            <input type="number" min="0" max="100" step="0.5" className={`${field} block w-24`} value={config.very_aesthetic_top ?? 5} onChange={(event) => set('very_aesthetic_top', Number(event.target.value))} /></label>
          <label className="text-xs text-slate-400" title="Includes the very aesthetic share">Aesthetic: top %
            <input type="number" min="0" max="100" step="0.5" className={`${field} block w-24`} value={config.aesthetic_top ?? 15} onChange={(event) => set('aesthetic_top', Number(event.target.value))} /></label>
        </>}
        {draft && <button className={button} disabled={save.isPending} onClick={() => save.mutate()}>Save settings</button>}
        {draft && <button className={quiet} onClick={() => setDraft(null)}>Discard</button>}
      </div>}
      <p className="text-xs text-slate-400">The best quality share includes the masterpiece share: with 5 and 10, the top 5% get masterpiece and the next 5% best quality.</p>
      {save.isError && <p role="alert" className="text-sm text-red-400">{errorText(save.error)}</p>}
      <p className="text-xs text-slate-400">
        {stats ? `Score statistics: ${number(stats.posts)} harvested posts, counted ${new Date(stats.built_at).toLocaleString()}.`
          : 'Scores have not been counted yet; the first run counts them (one pass over the harvested posts, a few minutes for millions of posts).'}
      </p>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <button className={button} disabled={running || Boolean(draft) || start.isPending}
          onClick={() => start.mutate({ rebuild })}>Apply to all planner collections</button>
        <label className="flex items-center gap-2 text-xs text-slate-300" title="Do this after harvesting more posts">
          <input type="checkbox" checked={rebuild} onChange={(event) => setRebuild(event.target.checked)} /> Recount scores first
        </label>
        {draft && <span className="text-xs text-amber-300">Save or discard the settings first.</span>}
      </div>
      {(job?.status && job.status !== 'idle') && <p role="status" className={`text-sm ${job.status === 'failed' ? 'text-red-400' : running ? 'text-blue-200' : 'text-green-300'}`}>{describeQualityJob(job)}</p>}
      {start.isError && <p role="alert" className="text-sm text-red-400">{errorText(start.error)}</p>}
      {table?.sites?.length > 0 && savedConfig && <details className="text-xs">
        <summary className="cursor-pointer text-slate-300">Cut-offs per year and rating ({savedConfig.metric === 'favorites' ? 'favorites' : 'score'}; <span className="text-amber-200">masterpiece</span> · <span className="text-sky-200">best quality</span>{savedConfig.low_quality_bottom > 0 && <> · <span className="text-rose-200">low quality</span></>}; small number = posts)</summary>
        <div className="mt-2 space-y-4">
          {table.sites.map((site) => (
            <div key={site.site} className="overflow-x-auto">
              <table className="text-xs">
                <thead><tr className="text-left text-slate-400">
                  <th className="px-2 py-1">{label(site.site)}</th><th className="px-2 py-1">All ratings</th>
                  {savedConfig.by_rating && site.ratings.map((rating) => <th key={rating} className="px-2 py-1">{label(rating)}</th>)}
                </tr></thead>
                <tbody>
                  <tr className="border-b border-slate-800"><td className="px-2 py-1 text-slate-400">All years</td><Cell cell={site.overall} config={savedConfig} />
                    {savedConfig.by_rating && site.ratings.map((rating) => <td key={rating} />)}</tr>
                  {site.years.map((year) => (
                    <tr key={year.year} className="border-b border-slate-900">
                      <td className="px-2 py-1 text-slate-400">{year.year || 'unknown'}</td>
                      <Cell cell={year.all} config={savedConfig} />
                      {savedConfig.by_rating && site.ratings.map((rating) => <Cell key={rating} cell={year.ratings[rating]} config={savedConfig} />)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      </details>}
    </section>
  );
}
