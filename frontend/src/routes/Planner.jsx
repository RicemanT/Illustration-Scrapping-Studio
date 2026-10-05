import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import QualityTagsPanel from '../components/QualityTagsPanel';

const field = 'rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm';
const button = 'rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-40';
const quiet = 'rounded border border-slate-700 px-3 py-2 text-sm disabled:opacity-40';
const SITES = ['danbooru', 'gelbooru', 'e621'];
const ACTIVE = ['queued', 'running', 'cancelling'];
const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  if (detail?.errors) return detail.errors.join('; ');
  return typeof detail === 'string' ? detail : String(error?.message || detail || 'Request failed');
};
const number = (value) => Number(value || 0).toLocaleString();

// Form values survive leaving the page; stored per browser only.
function usePersisted(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const raw = window.localStorage.getItem('planner:' + key);
      return raw === null ? initial : JSON.parse(raw);
    } catch { return initial; }
  });
  useEffect(() => {
    try { window.localStorage.setItem('planner:' + key, JSON.stringify(value)); } catch { /* storage unavailable */ }
  }, [key, value]);
  return [value, setValue];
}

async function readText(event) {
  const file = event.target.files?.[0];
  event.target.value = '';
  if (!file) return null;
  if (file.size > 20 * 1024 * 1024) throw new Error('Use a file no larger than 20 MiB');
  return new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
}

export default function Planner() {
  const client = useQueryClient();
  const [runId, setRunId] = usePersisted('runId', null);
  const [notice, setNotice] = useState('');
  const [fileError, setFileError] = useState('');
  const status = useQuery({
    queryKey: ['planner-status'],
    queryFn: async () => (await api.planner.status()).data,
    refetchInterval: (query) => {
      const data = query.state.data;
      return ACTIVE.includes(data?.harvest_job?.status) || data?.runs?.some((run) => run.status === 'running') ? 2000 : 15000;
    },
  });
  const refresh = () => client.invalidateQueries({ queryKey: ['planner-status'] });
  const importArtists = useMutation({ mutationFn: api.planner.importArtists, onSuccess: ({ data }) => { setNotice(`Artists: ${data.added} added, ${data.updated} updated, ${data.disabled} disabled.`); refresh(); } });
  const importCharacters = useMutation({ mutationFn: api.planner.importCharacters, onSuccess: ({ data }) => { setNotice(`Character targets: ${number(data.imported)} imported.`); refresh(); } });
  const [topCounts, setTopCounts] = usePersisted('topCounts', { danbooru: 3000, e621: 3000 });
  const [series, setSeries] = usePersisted('series', { danbooru: '', e621: '' });
  const [seriesMinPosts, setSeriesMinPosts] = usePersisted('seriesMinPosts', 30);
  const seriesJob = useQuery({ queryKey: ['planner-series'], queryFn: async () => (await api.planner.seriesStatus()).data,
    refetchInterval: (query) => query.state.data?.status === 'running' ? 1500 : false });
  const addSeries = useMutation({ mutationFn: () => api.planner.prioritySeries({ ...Object.fromEntries(['danbooru', 'e621'].map((family) => [family, series[family].split(/[\n,]/).map((item) => item.trim().replace(/ /g, '_')).filter(Boolean)])), min_posts: Number(seriesMinPosts) || 1 }),
    onSuccess: () => seriesJob.refetch() });
  const seriesRunning = seriesJob.data?.status === 'running';
  useEffect(() => { if (seriesJob.data?.status === 'completed') refresh(); }, [seriesJob.data?.status]);
  const [scope, setScope] = usePersisted('scope', '');
  const enableOnly = useMutation({ mutationFn: () => api.planner.enableOnly(scope.split('\n').map((line) => line.trim()).filter(Boolean)),
    onSuccess: ({ data }) => { setNotice(`Planning limited to ${number(data.enabled)} artists.${data.unknown.length ? ` Not on the list: ${data.unknown.slice(0, 20).join(', ')}${data.unknown.length > 20 ? '…' : ''}.` : ''}${data.ambiguous.length ? ` On several sites, so all were enabled: ${data.ambiguous.slice(0, 20).join(', ')}.` : ''}`); refresh(); } });
  const enableAll = useMutation({ mutationFn: api.planner.enableAll, onSuccess: ({ data }) => { setNotice(`Enabled ${number(data.enabled)} more artists; every listed artist is in scope again.`); refresh(); } });
  const clearPriority = useMutation({ mutationFn: api.planner.clearPriority, onSuccess: ({ data }) => { setNotice(`Priority cleared; ${number(data.removed)} series-only targets removed.`); refresh(); } });
  const fetchCharacters = useMutation({ mutationFn: () => api.planner.fetchCharacters({ danbooru: Number(topCounts.danbooru) || 0, e621: Number(topCounts.e621) || 0 }),
    onSuccess: ({ data }) => { setNotice(`Character targets: ${Object.entries(data.imported).map(([site, count]) => `${number(count)} ${site}`).join(', ')} fetched.`); refresh(); } });
  const runs = status.data?.runs || [];
  // A remembered run may be gone (another library, or older than the ten listed): fall back to the latest.
  useEffect(() => { if (runs.length && !runs.some((run) => run.id === runId)) setRunId(runs[0].id); }, [runs, runId]);
  const upload = (mutation) => async (event) => {
    setFileError(''); setNotice('');
    try { const text = await readText(event); if (text) mutation.mutate(text); } catch (error) { setFileError(error.message); }
  };

  return <div className="space-y-5 max-w-6xl">
    <div>
      <h1 className="text-xl font-semibold">Dataset planner</h1>
      <p className="text-sm text-slate-400">Choose each artist's training images from post metadata (tags, favorites, sizes) before downloading anything. The planner keeps its own data and never changes your collections.</p>
      {status.data?.path && <p className="text-xs text-slate-500" title="Inside the active library. Set ARTIST_PLANNER_PATH before starting the backend to store it elsewhere.">Planner data: {status.data.path}</p>}
    </div>
    {[status.error, importArtists.error, importCharacters.error, fetchCharacters.error, addSeries.error, clearPriority.error, enableOnly.error, enableAll.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {fileError && <p role="alert" className="text-red-400">{fileError}</p>}
    {notice && <p role="status" className="text-green-300">{notice}</p>}

    <section className="rounded border border-slate-800 p-4 space-y-3">
      <h2 className="font-semibold">1. Inputs</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
        <div className="space-y-1">
          <label className="block">Artists CSV <input type="file" accept=".csv,text/csv" aria-label="Artists CSV" className="block mt-1" disabled={importArtists.isPending} onChange={upload(importArtists)} /></label>
          <p className="text-xs text-slate-400">Columns: <code>site</code> (danbooru, gelbooru or e621) and <code>query_tag</code> (the exact site tag); <code>display_name</code>, <code>tag_id</code> and <code>post_count</code> are optional. Uploading again updates the list; artists no longer listed are disabled, and their harvested data is kept.</p>
          <pre className="text-xs text-slate-500">{'site,query_tag,display_name\ndanbooru,example_artist,example artist\ne621,another_artist,'}</pre>
        </div>
        <div className="space-y-2">
          <p>Character targets</p>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-slate-400">Top Danbooru<input type="number" min={0} max={50000} className={`${field} block w-24`} value={topCounts.danbooru} onChange={(event) => setTopCounts((old) => ({ ...old, danbooru: event.target.value }))} /></label>
            <label className="text-xs text-slate-400">Top e621<input type="number" min={0} max={50000} className={`${field} block w-24`} value={topCounts.e621} onChange={(event) => setTopCounts((old) => ({ ...old, e621: event.target.value }))} /></label>
            <button className={quiet} disabled={fetchCharacters.isPending} onClick={() => { setNotice(''); fetchCharacters.mutate(); }}>{fetchCharacters.isPending ? 'Fetching…' : 'Fetch most-posted characters'}</button>
          </div>
          <p className="text-xs text-slate-400">Fetches each site's characters by post count (Danbooru's list also covers Gelbooru), skipping placeholders such as fan_character. A count of 0 leaves that site's targets unchanged. Or upload your own CSV with <code>site</code> and <code>tag</code> columns in priority order; it replaces all targets.</p>
          <label className="block text-xs text-slate-400">Characters CSV <input type="file" accept=".csv,text/csv" aria-label="Characters CSV" className="block mt-1" disabled={importCharacters.isPending} onChange={upload(importCharacters)} /></label>
          <p className="pt-2">Priority series</p>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-slate-400">Danbooru / Gelbooru series<textarea className={`${field} block w-64 h-16`} placeholder="blue_archive, touhou, fate_(series)" value={series.danbooru} onChange={(event) => setSeries((old) => ({ ...old, danbooru: event.target.value }))} /></label>
            <label className="text-xs text-slate-400">e621 series<textarea className={`${field} block w-64 h-16`} placeholder="my_little_pony, helluva_boss" value={series.e621} onChange={(event) => setSeries((old) => ({ ...old, e621: event.target.value }))} /></label>
            <label className="text-xs text-slate-400">Posts at least<input type="number" min={1} className={`${field} block w-24`} value={seriesMinPosts} onChange={(event) => setSeriesMinPosts(event.target.value)} /></label>
            <button className={quiet} disabled={addSeries.isPending || seriesRunning || !(series.danbooru.trim() || series.e621.trim())} onClick={() => { setNotice(''); addSeries.mutate(); }}>{seriesRunning ? 'Looking up…' : 'Add as priority'}</button>
            <button className={quiet} disabled={clearPriority.isPending || seriesRunning} onClick={() => clearPriority.mutate()}>Clear priority</button>
          </div>
          {seriesJob.data?.status === 'running' && <p className="text-xs text-slate-300">Looking up series {seriesJob.data.done}/{seriesJob.data.total}{seriesJob.data.current ? ` · ${seriesJob.data.current}` : ''}</p>}
          {seriesJob.data?.status === 'failed' && <p role="alert" className="text-xs text-red-400">Series lookup failed: {seriesJob.data.error}</p>}
          {seriesJob.data?.status === 'completed' && Object.entries(seriesJob.data.results || {}).map(([site, result]) => <div key={site} className="text-xs text-green-300">
            <p>{site}: {number(result.priority)} priority characters ({number(result.added)} new targets). {Object.entries(result.series).map(([name, count]) => `${name} ${number(count)}`).join(' · ')}</p>
            {Object.keys(result.skipped || {}).length > 0 && <p className="text-amber-300">Skipped: {Object.entries(result.skipped).map(([name, why]) => `${name} (${why})`).join(', ')}</p>}
          </div>)}
          <p className="text-xs text-slate-400">Enter each site's series (copyright) tags; names differ between sites, for example <code>sonic_(series)</code> on Danbooru and <code>sonic_the_hedgehog_(series)</code> on e621. Danbooru characters are found from related tags and <code>name_(series)</code> tags, e621 characters from tag implications. They become priority targets: topped up first, with character need multiplied by the priority weight. Each artist's image count does not change.</p>
        </div>
      </div>
      <details className="text-sm"><summary className="cursor-pointer">Limit planning to some artists (for a pilot)</summary>
        <div className="mt-2 space-y-2">
          <p className="text-xs text-slate-400">One artist per line: the tag or display name, optionally with a site first (<code>e621,some_artist</code>). Other artists stay on the list but are left out of harvests, plans and downloads until you enable everyone again.</p>
          <textarea aria-label="Artists to keep in scope" className={`${field} block w-full h-28`} value={scope} onChange={(event) => setScope(event.target.value)} placeholder={'danbooru,example_artist\nanother artist'} />
          <div className="flex flex-wrap gap-2"><button className={quiet} disabled={enableOnly.isPending || !scope.trim()} onClick={() => { setNotice(''); enableOnly.mutate(); }}>Enable only these</button>
            <button className={quiet} disabled={enableAll.isPending} onClick={() => { setNotice(''); enableAll.mutate(); }}>Enable all listed artists</button></div>
        </div>
      </details>
      <table className="text-sm"><tbody>
        {SITES.map((site) => { const entry = status.data?.artists?.[site]; return <tr key={site}><td className="pr-4 text-slate-400">{site}</td>
          <td className="pr-4">{number(entry?.enabled)} {entry?.enabled === 1 ? 'artist' : 'artists'}{entry?.disabled ? ` (${number(entry.disabled)} disabled)` : ''}</td>
          <td className="pr-4">{number(status.data?.posts?.[site])} posts harvested</td>
          <td className="text-slate-400">{Object.entries(entry?.harvest || {}).map(([key, value]) => `${number(value)} ${key}`).join(' · ')}</td></tr>; })}
        <tr><td className="pr-4 text-slate-400">scope</td><td colSpan={3}>{number(Object.values(status.data?.artists || {}).reduce((sum, entry) => sum + (entry.enabled || 0), 0))} of {number(status.data?.listed_artists)} listed artists enabled for planning</td></tr>
        <tr><td className="pr-4 text-slate-400">characters</td><td colSpan={3}>{number(status.data?.characters?.danbooru)} Danbooru/Gelbooru · {number(status.data?.characters?.e621)} e621 targets{(status.data?.priority_characters?.danbooru || status.data?.priority_characters?.e621) ? ` · priority: ${number(status.data?.priority_characters?.danbooru)} Danbooru/Gelbooru, ${number(status.data?.priority_characters?.e621)} e621` : ''}</td></tr>
      </tbody></table>
    </section>

    <HarvestPanel status={status.data} onChange={refresh} />
    <RunPanel status={status.data} runId={runId} setRunId={setRunId} onChange={refresh} />
    {runId && <ReviewPanel runId={runId} />}
    <DeliveryPanel status={status.data} runId={runId} onChange={refresh} />
    <QualityTagsPanel />
  </div>;
}

function HarvestPanel({ status, onChange }) {
  const [sites, setSites] = usePersisted('harvestSites', SITES);
  const [maxPosts, setMaxPosts] = usePersisted('harvestMaxPosts', 2000);
  const [refreshAll, setRefreshAll] = useState(false);
  const job = status?.harvest_job;
  const running = ACTIVE.includes(job?.status);
  const start = useMutation({ mutationFn: () => api.planner.harvest({ sites, max_posts_per_artist: Number(maxPosts), refresh: refreshAll }), onSuccess: onChange });
  const cancel = useMutation({ mutationFn: api.planner.cancelHarvest, onSuccess: onChange });
  return <section className="rounded border border-slate-800 p-4 space-y-3">
    <h2 className="font-semibold">2. Harvest metadata</h2>
    <p className="text-xs text-slate-400">Collects tags, scores and sizes for every enabled artist, newest posts first. No images are downloaded. Sites run in parallel at their normal request pace; a stopped or interrupted harvest resumes where it left off, and failed artists are retried on the next start. Gelbooru needs its API key in Settings.</p>
    <div className="flex flex-wrap items-end gap-4 text-sm">
      {SITES.map((site) => <label key={site} className="flex items-center gap-2"><input type="checkbox" checked={sites.includes(site)} disabled={running}
        onChange={(event) => setSites((old) => event.target.checked ? [...old, site] : old.filter((item) => item !== site))} />{site}</label>)}
      <label>Posts per artist at most <input type="number" min={20} max={100000} className={`${field} w-28 ml-2`} value={maxPosts} disabled={running} onChange={(event) => setMaxPosts(event.target.value)} /></label>
      <label className="flex items-center gap-2" title="Harvest finished artists again from their newest post"><input type="checkbox" checked={refreshAll} disabled={running} onChange={(event) => setRefreshAll(event.target.checked)} />Refresh finished artists</label>
      {running
        ? <button className={button} disabled={cancel.isPending || job.status === 'cancelling'} onClick={() => cancel.mutate()}>Stop harvest</button>
        : <button className={button} disabled={start.isPending || !sites.length} onClick={() => start.mutate()}>{job ? 'Start / resume harvest' : 'Start harvest'}</button>}
    </div>
    {[start.error, cancel.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {job && <div className="text-sm space-y-1">
      <p>Harvest #{job.id}: {job.status}{job.error ? ` — ${job.error}` : ''}</p>
      {Object.entries(job.progress?.sites || {}).map(([site, state]) => <div key={site}>
        <p className="text-slate-300">{site}: {number(state.done)}/{number(state.total)} artists · {number(state.posts)} posts this job · {number(state.errors)} errors{state.current ? ` · now ${state.current}` : ''}</p>
        {state.blocked && <p className="text-red-400 text-xs">{state.blocked}</p>}
        <div className="h-1 rounded bg-[#0c1219] overflow-hidden"><div className="h-full bg-blue-500" style={{ width: `${state.total ? Math.round(state.done * 100 / state.total) : 0}%` }} /></div>
      </div>)}
      <details><summary className="text-xs text-slate-400 cursor-pointer">Log</summary><pre className="max-h-40 overflow-auto text-xs text-slate-400 whitespace-pre-wrap">{(job.progress?.log || []).join('\n')}</pre></details>
    </div>}
  </section>;
}

const NUMBER_FIELDS = [
  ['min_images', 'Drop artists below (usable images)'], ['max_images', 'Images per artist at most'],
  ['exposures_per_artist', 'Training samples per artist'], ['max_repeats', 'Repeats at most'],
  ['min_short_side', 'Shortest side at least (px)'], ['max_aspect_ratio', 'Aspect ratio at most'],
  ['character_floor', 'Images per target character'], ['character_share_cap', 'One character’s share of an artist at most (0–1)'],
  ['topup_max_per_artist', 'Character top-ups per artist at most'], ['candidate_pool', 'Candidates kept per artist'],
  ['min_year', 'Posts from year (blank = any)'], ['newest_posts_per_artist', 'Only each artist’s newest posts (0 = all)'],
];
const WEIGHT_FIELDS = ['weight_quality', 'weight_novelty', 'weight_character', 'weight_rarity', 'weight_boost', 'priority_character_boost'];

function RunPanel({ status, runId, setRunId, onChange }) {
  const defaults = useQuery({ queryKey: ['planner-defaults'], queryFn: async () => (await api.planner.defaults()).data, staleTime: Infinity });
  const [config, setConfig] = usePersisted('config', null);
  // Saved settings keep their values; settings added in newer versions take their defaults.
  useEffect(() => { if (defaults.data) setConfig((old) => old ? { ...defaults.data, ...old } : defaults.data); }, [defaults.data]);
  const runs = status?.runs || [];
  const selected = runs.find((run) => run.id === runId);
  const running = runs.some((run) => run.status === 'running');
  const harvesting = ACTIVE.includes(status?.harvest_job?.status);
  const [bannedRemoved, setBannedRemoved] = useState(0);
  const start = useMutation({ mutationFn: () => api.planner.run(config), onSuccess: ({ data }) => { setRunId(data.id); setBannedRemoved(data.banned_removed || 0); onChange(); } });
  const exportRun = useMutation({ mutationFn: () => api.planner.exportRun(runId) });
  const set = (key, value) => setConfig((old) => ({ ...old, [key]: value }));
  return <section className="rounded border border-slate-800 p-4 space-y-3">
    <h2 className="font-semibold">3. Plan</h2>
    <p className="text-xs text-slate-400">Each artist picks images in turns by quality (within the artist), new content, character need, tag rarity and a bonus for boost tags. Characters are counted across all artists, so a needed character can come from anyone who draws it. Re-run after changing settings or reviewing; earlier runs are kept.</p>
    {config && <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
      {NUMBER_FIELDS.map(([key, label]) => <label key={key} className="flex flex-col gap-1"><span className="text-slate-400">{label}</span>
        <input type="number" step="any" className={field} value={config[key] ?? ''} onChange={(event) => set(key, event.target.value === '' ? null : Number(event.target.value))} /></label>)}
      <label className="flex flex-col gap-1" title="Sites list credited artists alphabetically, not by role, so every credited artist is kept in the manifest. Larger group collaborations are skipped."><span className="text-slate-400">Credited artists per post at most</span>
        <input type="number" min={1} className={field} value={config.max_credited_artists ?? 2} onChange={(event) => set('max_credited_artists', Number(event.target.value) || 1)} /></label>
      <label className="flex items-center gap-2" title="Import extracts up to three frames from each video, animation or ugoira"><input type="checkbox" checked={config.include_motion ?? true} onChange={(event) => set('include_motion', event.target.checked)} />Include videos, animations and ugoira</label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={config.character_topup} onChange={(event) => set('character_topup', event.target.checked)} />Top up characters below their target</label>
      <label className="flex flex-col gap-1"><span className="text-slate-400">Ratings (blank = all)</span>
        <input className={field} value={(config.allowed_ratings || []).join(', ')} placeholder="general, sensitive, safe, questionable, explicit"
          onChange={(event) => { const values = event.target.value.split(',').map((item) => item.trim()).filter(Boolean); set('allowed_ratings', values.length ? values : null); }} /></label>
    </div>}
    {config && <details className="text-sm"><summary className="cursor-pointer text-slate-300">Score weights and tag lists</summary>
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mt-3">{WEIGHT_FIELDS.map((key) => <label key={key} className="flex flex-col gap-1"><span className="text-slate-400">{key === 'priority_character_boost' ? 'priority character ×' : key.replace('weight_', '')}</span>
        <input type="number" step="0.1" min={0} className={field} value={config[key]} onChange={(event) => set(key, Number(event.target.value))} /></label>)}</div>
      <p className="text-xs text-slate-400 mt-3">Danbooru and e621 use different tag names, so each has its own lists; Gelbooru uses the Danbooru lists. Blocked tags skip a post. Boost tags favor useful but underrepresented concepts such as unusual camera angles, action, interaction, environments and lighting. Tags get renamed over time: use Check to compare a list with the live site.</p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2">
        {['danbooru', 'e621'].flatMap((family) => ['blocked', 'boost'].map((kind) => <TagListEditor key={`${kind}-${family}`} family={family}
          label={`${kind === 'blocked' ? 'Blocked' : 'Boost'} tags · ${family === 'danbooru' ? 'Danbooru and Gelbooru' : 'e621'}`}
          value={config[`${kind}_tags_${family}`] || []} onChange={(tags) => set(`${kind}_tags_${family}`, tags)} />))}
      </div>
      <button className={`${quiet} mt-2`} onClick={() => setConfig(defaults.data)}>Reset to defaults</button>
    </details>}
    <div className="flex flex-wrap gap-2 items-center">
      <button className={button} disabled={!config || running || harvesting || start.isPending} onClick={() => start.mutate()}>{running ? 'Planning…' : 'Run plan'}</button>
      {harvesting && <span className="text-xs text-slate-400">Wait for the harvest to finish before planning.</span>}
      {runs.length > 0 && <select aria-label="Plan run" className={field} value={runId || ''} onChange={(event) => setRunId(Number(event.target.value))}>
        {runs.map((run) => <option key={run.id} value={run.id}>Run #{run.id} · {run.status} · {run.created_at.slice(0, 16).replace('T', ' ')}</option>)}
      </select>}
      {selected?.status === 'completed' && <button className={quiet} disabled={exportRun.isPending} onClick={() => exportRun.mutate()}>Export manifest</button>}
      {selected?.status === 'completed' && <button className={quiet} onClick={() => setConfig({ ...defaults.data, ...selected.config })}>Load this run’s settings</button>}
    </div>
    {bannedRemoved > 0 && <p className="text-sm text-amber-300">{number(bannedRemoved)} images you removed from planner collections are now banned, so this run will not pick them again.</p>}
    {[start.error, exportRun.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {exportRun.data && <p className="text-sm text-green-300">Exported to {exportRun.data.data.path}: {exportRun.data.data.files.join(', ')}</p>}
    {selected?.error && <p className="text-red-400 text-sm">{selected.error}</p>}
    {selected?.summary && <RunSummary summary={selected.summary} />}
  </section>;
}

const TAG_STATUS = { alias: 'renamed', deprecated: 'deprecated', empty: 'no posts', missing: 'does not exist' };

function TagListEditor({ family, label, value, onChange }) {
  const [text, setText] = useState(value.join('\n'));
  useEffect(() => { setText(value.join('\n')); }, [value.join('\n')]);
  const check = useMutation({ mutationFn: () => api.planner.checkTags(family, value) });
  const problems = check.data?.data.items.filter((item) => item.status !== 'ok') || [];
  const renamed = problems.filter((item) => item.status === 'alias');
  const unusable = problems.filter((item) => item.status !== 'alias');
  const commit = (tags) => { check.reset(); onChange(tags); };
  return <div className="flex flex-col gap-1">
    <span className="text-slate-400">{label} ({value.length})</span>
    <textarea className={`${field} h-36`} value={text} onChange={(event) => setText(event.target.value)}
      onBlur={() => commit(text.split(/[\n,]/).map((item) => item.trim().replace(/ /g, '_')).filter(Boolean))} />
    <div className="flex flex-wrap items-center gap-2">
      <button className={quiet} disabled={check.isPending || !value.length} onClick={() => check.mutate()}>{check.isPending ? 'Checking…' : `Check on ${family === 'danbooru' ? 'Danbooru' : 'e621'}`}</button>
      {check.data && !problems.length && <span className="text-xs text-green-300">All {value.length} tags are current.</span>}
      {renamed.length > 0 && <button className={quiet} onClick={() => commit(value.map((tag) => renamed.find((item) => item.tag === tag)?.replacement || tag))}>Use current names ({renamed.length})</button>}
      {unusable.length > 0 && <button className={quiet} onClick={() => commit(value.filter((tag) => !unusable.some((item) => item.tag === tag)))}>Remove unusable ({unusable.length})</button>}
    </div>
    {check.error && <p role="alert" className="text-xs text-red-400">{errorText(check.error)}</p>}
    {problems.length > 0 && <ul className="text-xs text-amber-300 max-h-28 overflow-auto">{problems.map((item) => <li key={item.tag}>{item.tag}: {TAG_STATUS[item.status]}{item.replacement ? ` → ${item.replacement}` : ''}</li>)}</ul>}
  </div>;
}

const DELIVERY_STATES = ['done', 'skipped', 'filtered', 'missing', 'error', 'pending'];

function DeliveryPanel({ status, runId, onChange }) {
  const [prefix, setPrefix] = usePersisted('deliveryPrefix', 'Planner');
  const deliveryId = status?.delivery_id;
  const job = useQuery({ queryKey: ['planner-delivery', deliveryId], enabled: Boolean(deliveryId), queryFn: async () => (await api.planner.delivery(deliveryId)).data,
    refetchInterval: (query) => ACTIVE.includes(query.state.data?.status) ? 2000 : false });
  const data = job.data;
  const active = ACTIVE.includes(data?.status);
  const run = status?.runs?.find((item) => item.id === runId);
  const harvesting = ACTIVE.includes(status?.harvest_job?.status);
  const after = () => { onChange(); job.refetch(); };
  const start = useMutation({ mutationFn: () => api.planner.deliver(runId, prefix), onSuccess: after });
  const resume = useMutation({ mutationFn: () => api.planner.resumeDelivery(deliveryId), onSuccess: after });
  const errorCount = Object.entries(data?.counts || {}).filter(([key]) => key.endsWith(':error')).reduce((sum, [, n]) => sum + n, 0);
  const problemCount = Object.entries(data?.counts || {}).filter(([key]) => /:(error|missing|filtered|skipped)$/.test(key)).reduce((sum, [, n]) => sum + n, 0);
  const [showProblems, setShowProblems] = useState(false);
  const problems = useQuery({ queryKey: ['planner-problems', deliveryId, data?.status, problemCount], enabled: Boolean(deliveryId) && showProblems, queryFn: async () => (await api.planner.deliveryProblems(deliveryId)).data });
  const cancel = useMutation({ mutationFn: api.planner.cancelDelivery, onSuccess: after });
  const layout = useMutation({ mutationFn: () => api.planner.trainingLayout(deliveryId) });
  const prunePreview = useMutation({ mutationFn: () => api.planner.prunePreview(deliveryId) });
  const pruneApply = useMutation({ mutationFn: () => api.planner.pruneApply(deliveryId), onSuccess: () => prunePreview.reset() });
  const sites = Object.keys(data?.progress?.sites || {});
  return <section className="rounded border border-slate-800 p-4 space-y-3">
    <h2 className="font-semibold">5. Download selected images</h2>
    <p className="text-xs text-slate-400">Downloads the chosen run into collections: one group per site named “&lt;prefix&gt; &lt;site&gt;” and one artist collection per artist, with the usual sidecars, artist trigger, duplicate checks and frame extraction. Planner collections are protected from Sync All, scheduled and group syncs. Metadata is refreshed in batches of 100 posts; downloads use the normal request pace and worker count, so large plans take days and need plenty of disk space. Stopping keeps finished downloads, and Resume continues the rest. Delivering a run again reuses its collections and skips posts already downloaded.</p>
    <div className="flex flex-wrap items-end gap-2 text-sm">
      <label className="text-xs text-slate-400">Group name prefix<input className={`${field} block w-40`} value={prefix} maxLength={80} onChange={(event) => setPrefix(event.target.value)} /></label>
      <button className={button} disabled={!run || run.status !== 'completed' || active || harvesting || start.isPending || !prefix.trim()} onClick={() => start.mutate()}>Download run #{runId || '—'}</button>
      {active && <button className={quiet} disabled={cancel.isPending || data.status === 'cancelling'} onClick={() => cancel.mutate()}>Stop</button>}
      {data && !active && (data.status !== 'completed' || errorCount > 0) && <button className={quiet} disabled={resume.isPending || harvesting} onClick={() => resume.mutate()}>{data.status === 'completed' ? `Retry ${number(errorCount)} failed` : `Resume download #${data.id}`}</button>}
      {data && !active && <button className={quiet} disabled={layout.isPending} onClick={() => layout.mutate()}>Export training layout</button>}
      {data && !active && <button className={quiet} disabled={prunePreview.isPending || pruneApply.isPending} onClick={() => { pruneApply.reset(); prunePreview.mutate(); }}>Remove images no longer selected…</button>}
      {harvesting && <span className="text-xs text-slate-400">Wait for the harvest to finish first.</span>}
    </div>
    {[start.error, resume.error, cancel.error, layout.error, job.error, prunePreview.error, pruneApply.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {prunePreview.data && <div className="rounded border border-amber-700/60 p-3 text-sm space-y-2">
      {prunePreview.data.data.images ? <>
        <p>{number(prunePreview.data.data.images)} images in {number(prunePreview.data.data.collections)} collections came from an earlier planner download but are not selected by run #{data?.run_id}, for example posts you banned since. Removing them keeps your training folders matching the plan.</p>
        <p className="text-xs text-slate-400">Images you imported yourself are never included. Removed images go to each collection's recovery, which keeps only the latest removal: this replaces any earlier "Recover last deletion" batch in those collections.</p>
        <div className="flex gap-2"><button className="rounded border border-red-800 px-3 py-2 text-sm text-red-200 disabled:opacity-40" disabled={pruneApply.isPending} onClick={() => pruneApply.mutate()}>{pruneApply.isPending ? 'Removing…' : `Remove ${number(prunePreview.data.data.images)} images`}</button>
          <button className={quiet} onClick={() => prunePreview.reset()}>Cancel</button></div>
      </> : <p>Every planner image in these collections is still selected by run #{data?.run_id}. Nothing to remove.</p>}
    </div>}
    {pruneApply.data && <p className="text-sm text-green-300">Removed {number(pruneApply.data.data.removed)} images. Export the training layout again to refresh image counts.</p>}
    <details className="text-xs text-slate-400"><summary className="cursor-pointer">Check styles with a GPU (optional)</summary>
      <div className="mt-2 space-y-1"><p>After a download, <code>tools/planner_style_check.py</code> flags images far from their artist's usual style, such as sketches, photos, 3D renders or guest art. It runs outside the app in any Python environment with torch, torchvision, numpy and Pillow, on one GPU. Flagged images show up in Review above.</p>
        <pre className="whitespace-pre-wrap text-slate-300">python tools/planner_style_check.py --library "{status?.library || '<library folder>'}"{status?.custom_path ? ` --planner "${status.path}"` : ''} --pause 0.1</pre>
        <p>Add <code>--device cuda:1</code> to pick a GPU, <code>--ban</code> to ban everything flagged, or <code>--threshold 4</code> to flag fewer images. After banning, run the plan again, download it and remove images no longer selected.</p></div>
    </details>
    {layout.data && <p className="text-sm text-green-300">Training layout written to {layout.data.data.path}: {layout.data.data.files.join(', ')}. dataset.toml has diffusion-pipe [[directory]] blocks; repeats follow the images left in each folder, so artists you trimmed by hand keep their share of training.</p>}
    {data && <div className="text-sm space-y-1">
      <p>Download #{data.id} of run #{data.run_id}: {data.status}{data.error ? ` — ${data.error}` : ''}</p>
      {sites.map((site) => { const total = data.progress.sites[site].total || 0; const left = data.counts?.[`${site}:pending`] || 0; const state = data.progress.sites[site];
        return <div key={site}>
          <p className="text-slate-300">{site}: {number(total - left)}/{number(total)} posts · {DELIVERY_STATES.filter((key) => key !== 'pending' && data.counts?.[`${site}:${key}`]).map((key) => `${number(data.counts[`${site}:${key}`])} ${key}`).join(' · ') || 'starting'}{state.current ? ` · now ${state.current}` : ''}</p>
          {state.blocked && <p className="text-red-400 text-xs">{state.blocked}</p>}
          <div className="h-1 rounded bg-[#0c1219] overflow-hidden"><div className="h-full bg-blue-500" style={{ width: `${total ? Math.round((total - left) * 100 / total) : 0}%` }} /></div>
        </div>; })}
      {problemCount > 0 && <details open={showProblems} onToggle={(event) => setShowProblems(event.currentTarget.open)}><summary className="text-xs text-slate-300 cursor-pointer">Posts that did not add an image ({number(problemCount)})</summary>
        {problems.error && <p role="alert" className="text-xs text-red-400">{errorText(problems.error)}</p>}
        <div className="max-h-48 overflow-auto text-xs divide-y divide-slate-800">{problems.data?.items.map((item) => <div key={`${item.site}-${item.remote_id}`} className="py-1 flex flex-wrap gap-x-3">
          <span className={item.status === 'error' ? 'text-red-300' : 'text-slate-400'}>{item.status}</span><a className="text-blue-300" href={item.url} target="_blank" rel="noreferrer">{item.site} #{item.remote_id}</a><span>{item.display_name}</span><span className="text-slate-400 min-w-0 break-words">{item.reason}</span></div>)}</div>
        <p className="text-xs text-slate-400 mt-1">Errors can be retried. Missing posts were deleted from the site; filtered ones are below the collection's quality floor. Neither can be fixed by retrying.</p></details>}
      <p className="text-xs text-slate-400">done = new images added · skipped = already in the collection · filtered = below the collection's quality floor · missing = deleted from the site since the harvest.</p>
      <details><summary className="text-xs text-slate-400 cursor-pointer">Log</summary><pre className="max-h-40 overflow-auto text-xs text-slate-400 whitespace-pre-wrap">{(data.progress?.log || []).join('\n')}</pre></details>
    </div>}
  </section>;
}

function RunSummary({ summary }) {
  return <div className="space-y-3 text-sm">
    <p>{number(summary.artists_kept)} artists · {number(summary.images)} images · {number(summary.samples_per_pass)} training samples per pass · planned in {summary.seconds}s</p>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {Object.entries(summary.families || {}).map(([family, data]) => <div key={family} className="rounded border border-slate-800 p-3 space-y-1">
        <p className="font-semibold">{family === 'danbooru' ? 'Danbooru + Gelbooru' : 'e621'}</p>
        <p>{number(data.artists_kept)} {data.artists_kept === 1 ? 'artist' : 'artists'} kept{Object.entries(data.artists_dropped || {}).map(([reason, count]) => ` · ${number(count)} dropped (${reason.replace(/_/g, ' ')})`).join('')}</p>
        <p>{number(data.images)} images ({Object.entries(data.roles || {}).map(([role, count]) => `${number(count)} ${role.replace('_', ' ')}`).join(', ')}) · {number(data.samples_per_pass)} samples</p>
        <p>Characters: {number(data.characters_at_floor)} at target · {number(data.characters_partial)} partial · {number(data.characters_missing)} absent of {number(data.character_targets)}</p>
        <p className="text-xs text-slate-400">Posts skipped: {Object.entries(data.rejected_posts || {}).sort((a, b) => b[1] - a[1]).map(([reason, count]) => `${reason.replace(/_/g, ' ')} ${number(count)}`).join(' · ') || 'none'}</p>
        {data.unmet_characters?.length > 0 && <details><summary className="text-xs text-slate-400 cursor-pointer">Characters below target (highest priority first)</summary>
          <p className="text-xs text-slate-400 max-h-40 overflow-auto">{data.unmet_characters.map(([tag, count]) => `${tag} (${count})`).join(', ')}</p></details>}
      </div>)}
    </div>
  </div>;
}

function ReviewPanel({ runId }) {
  const [site, setSite] = usePersisted('reviewSite', '');
  const [runStatus, setRunStatus] = usePersisted('reviewStatus', '');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [artistId, setArtistId] = useState(null);
  const [flagged, setFlagged] = useState(false);
  const params = { run_id: runId, site: site || undefined, run_status: runStatus || undefined, q: search || undefined, flagged: flagged || undefined, offset, limit: 50 };
  const artists = useQuery({ queryKey: ['planner-artists', params], queryFn: async () => (await api.planner.artists(params)).data, placeholderData: (old) => old });
  useEffect(() => setOffset(0), [site, runStatus, search, runId, flagged]);
  return <section className="rounded border border-slate-800 p-4 space-y-3">
    <h2 className="font-semibold">4. Review run #{runId}</h2>
    <p className="text-xs text-slate-400">Lock an image to always include it, or ban it to never include it. Locks and bans apply from the next plan run. Thumbnails are fetched by the backend and cached in the planner folder.</p>
    <div className="flex flex-wrap gap-2">
      <input className={field} placeholder="Search artists" value={search} onChange={(event) => setSearch(event.target.value)} />
      <select className={field} value={site} onChange={(event) => setSite(event.target.value)}><option value="">All sites</option>{SITES.map((item) => <option key={item}>{item}</option>)}</select>
      <select className={field} value={runStatus} onChange={(event) => setRunStatus(event.target.value)}><option value="">Kept and dropped</option><option value="kept">Kept</option><option value="dropped">Dropped</option></select>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={flagged} onChange={(event) => setFlagged(event.target.checked)} />Only artists with style flags</label>
    </div>
    {artists.error && <p role="alert" className="text-red-400">{errorText(artists.error)}</p>}
    <div className="grid grid-cols-1 md:grid-cols-[18rem_1fr] gap-4">
      <div className="space-y-2">
        <div className="max-h-[36rem] overflow-auto divide-y divide-slate-800 text-sm">
          {artists.data?.items.map((item) => <button key={item.id} onClick={() => setArtistId(item.id)} className={`block w-full text-left py-1.5 px-1 ${item.id === artistId ? 'text-blue-300' : ''}`}>
            {item.display_name} <span className="text-xs text-slate-500">{item.site}</span>
            <span className="block text-xs text-slate-400">{item.run_status === 'kept' ? `${item.selected} images × ${item.repeats}` : item.run_status === 'dropped' ? `dropped: ${String(item.run_reason).replace(/_/g, ' ')} (${item.usable} usable)` : `${item.harvest_status}, not in this run`}{item.style_flags ? ` · ${item.style_flags} style flag${item.style_flags === 1 ? '' : 's'}` : ''}</span>
          </button>)}
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <button className={quiet} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
          <span>{artists.data ? `${offset + 1}–${Math.min(offset + 50, artists.data.total)} of ${number(artists.data.total)}` : ''}</span>
          <button className={quiet} disabled={!artists.data || offset + 50 >= artists.data.total} onClick={() => setOffset(offset + 50)}>Next</button>
        </div>
      </div>
      {artistId ? <ArtistReview artistId={artistId} runId={runId} /> : <p className="text-sm text-slate-400">Choose an artist to see their selected images and runners-up.</p>}
    </div>
  </section>;
}

function ArtistReview({ artistId, runId }) {
  const client = useQueryClient();
  const detail = useQuery({ queryKey: ['planner-artist', artistId, runId], queryFn: async () => (await api.planner.artist(artistId, runId)).data });
  const override = useMutation({ mutationFn: api.planner.override, onSuccess: () => client.invalidateQueries({ queryKey: ['planner-artist', artistId, runId] }) });
  if (detail.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (detail.error) return <p role="alert" className="text-red-400">{errorText(detail.error)}</p>;
  const { artist, run, selected, runners_up: runnersUp } = detail.data;
  const act = (item, action) => override.mutate({ artist_id: artist.id, site: item.site, remote_id: item.remote_id, action });
  return <div className="space-y-3">
    <p className="text-sm">{artist.display_name} <span className="text-slate-400">({artist.site}: {artist.tag}) · {number(artist.harvested_posts)} posts harvested{run ? ` · ${run.usable} usable · ${run.selected} selected × ${run.repeats} repeats` : ''}</span></p>
    {override.error && <p role="alert" className="text-red-400">{errorText(override.error)}</p>}
    <h3 className="text-sm font-semibold">Selected ({selected.length})</h3>
    <Tiles artistId={artist.id} items={selected} onAction={act} busy={override.isPending} />
    <h3 className="text-sm font-semibold">Most popular posts not selected</h3>
    <p className="text-xs text-slate-400">Includes posts the filters skipped (low resolution, comics, and so on). Locking one includes it anyway.</p>
    <Tiles artistId={artist.id} items={runnersUp} onAction={act} busy={override.isPending} />
  </div>;
}

function Tiles({ artistId, items, onAction, busy }) {
  if (!items.length) return <p className="text-xs text-slate-400">None.</p>;
  return <div className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-6 gap-2">
    {items.map((item) => {
      const reasons = item.reasons ? Object.entries(item.reasons).map(([key, value]) => `${key}: ${value}`).join('\n') : '';
      const url = item.site === 'e621' ? `https://e621.net/posts/${item.remote_id}` : item.site === 'gelbooru' ? `https://gelbooru.com/index.php?page=post&s=view&id=${item.remote_id}` : `https://danbooru.donmai.us/posts/${item.remote_id}`;
      return <div key={`${item.site}-${item.remote_id}`} className={`rounded border p-1 text-xs space-y-1 ${item.override === 'lock' ? 'border-green-600' : item.override === 'ban' ? 'border-red-700 opacity-60' : item.style_flag ? 'border-amber-500' : 'border-slate-800'}`}>
        <a href={url} target="_blank" rel="noreferrer" title={`${item.characters || 'no character tags'}\n${item.width}×${item.height} · ${item.rating}\n${reasons}`}>
          <img src={backendAssetUrl(`/api/planner/thumbs/${artistId}/${item.site}/${item.remote_id}`)} alt="" loading="lazy" className="w-full h-28 object-contain bg-black/30" />
        </a>
        {item.style_flag && !item.override && <div className="text-amber-300" title={`Style distance ${item.style_flag.distance.toFixed(3)} from this artist's median`}>Off-style (z {item.style_flag.score.toFixed(1)})</div>}
        <div className="flex justify-between text-slate-400"><span>{item.role ? item.role.replace('_', ' ') : `♥ ${item.fav_count ?? item.score ?? 0}`}</span>{item.gain != null && <span title={reasons}>{item.gain.toFixed(2)}</span>}</div>
        <div className="flex gap-1">
          {item.override !== 'lock' && <button className="flex-1 rounded bg-green-900/60 px-1 disabled:opacity-40" disabled={busy} onClick={() => onAction(item, 'lock')}>Lock</button>}
          {item.override !== 'ban' && <button className="flex-1 rounded bg-red-900/60 px-1 disabled:opacity-40" disabled={busy} onClick={() => onAction(item, 'ban')}>Ban</button>}
          {item.override && <button className="flex-1 rounded border border-slate-700 px-1 disabled:opacity-40" disabled={busy} onClick={() => onAction(item, 'clear')}>Clear</button>}
        </div>
      </div>;
    })}
  </div>;
}
