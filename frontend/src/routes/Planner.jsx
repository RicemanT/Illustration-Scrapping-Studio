import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';

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

async function readText(event) {
  const file = event.target.files?.[0];
  event.target.value = '';
  if (!file) return null;
  if (file.size > 20 * 1024 * 1024) throw new Error('Use a file no larger than 20 MiB');
  return new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
}

export default function Planner() {
  const client = useQueryClient();
  const [runId, setRunId] = useState(null);
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
  const runs = status.data?.runs || [];
  useEffect(() => { if (!runId && runs.length) setRunId(runs[0].id); }, [runs, runId]);
  const upload = (mutation) => async (event) => {
    setFileError(''); setNotice('');
    try { const text = await readText(event); if (text) mutation.mutate(text); } catch (error) { setFileError(error.message); }
  };

  return <div className="space-y-5 max-w-6xl">
    <div>
      <h1 className="text-xl font-semibold">Dataset planner</h1>
      <p className="text-sm text-slate-400">Choose each artist's training images from post metadata before downloading anything. Planner data lives in {status.data?.path || 'the library planner folder'} and never changes your collections.</p>
    </div>
    {[status.error, importArtists.error, importCharacters.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {fileError && <p role="alert" className="text-red-400">{fileError}</p>}
    {notice && <p role="status" className="text-green-300">{notice}</p>}

    <section className="rounded border border-slate-800 p-4 space-y-3">
      <h2 className="font-semibold">1. Inputs</h2>
      <p className="text-xs text-slate-400">artists.csv needs site, display_name and query_tag columns; artists missing from a new upload are disabled, not deleted. characters.csv needs site and tag, in priority order. Both are built by the artist list project.</p>
      <div className="flex flex-wrap gap-6 text-sm">
        <label className="space-y-1"><span className="block">Artists (artists.csv)</span><input type="file" accept=".csv,text/csv" aria-label="Artists CSV" disabled={importArtists.isPending} onChange={upload(importArtists)} /></label>
        <label className="space-y-1"><span className="block">Character targets (characters.csv)</span><input type="file" accept=".csv,text/csv" aria-label="Characters CSV" disabled={importCharacters.isPending} onChange={upload(importCharacters)} /></label>
      </div>
      <table className="text-sm"><tbody>
        {SITES.map((site) => { const entry = status.data?.artists?.[site]; return <tr key={site}><td className="pr-4 text-slate-400">{site}</td>
          <td className="pr-4">{number(entry?.enabled)} artists{entry?.disabled ? ` (${number(entry.disabled)} disabled)` : ''}</td>
          <td className="pr-4">{number(status.data?.posts?.[site])} posts harvested</td>
          <td className="text-slate-400">{Object.entries(entry?.harvest || {}).map(([key, value]) => `${number(value)} ${key}`).join(' · ')}</td></tr>; })}
        <tr><td className="pr-4 text-slate-400">characters</td><td colSpan={3}>{number(status.data?.characters?.danbooru)} Danbooru/Gelbooru · {number(status.data?.characters?.e621)} e621 targets</td></tr>
      </tbody></table>
    </section>

    <HarvestPanel status={status.data} onChange={refresh} />
    <RunPanel status={status.data} runId={runId} setRunId={setRunId} onChange={refresh} />
    {runId && <ReviewPanel runId={runId} />}
  </div>;
}

function HarvestPanel({ status, onChange }) {
  const [sites, setSites] = useState(SITES);
  const [maxPosts, setMaxPosts] = useState(2000);
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
  ['min_year', 'Posts from year (blank = any)'],
];
const WEIGHT_FIELDS = ['weight_quality', 'weight_novelty', 'weight_character', 'weight_rarity', 'weight_boost'];

function RunPanel({ status, runId, setRunId, onChange }) {
  const defaults = useQuery({ queryKey: ['planner-defaults'], queryFn: async () => (await api.planner.defaults()).data, staleTime: Infinity });
  const [config, setConfig] = useState(null);
  useEffect(() => { if (defaults.data && !config) setConfig(defaults.data); }, [defaults.data, config]);
  const runs = status?.runs || [];
  const selected = runs.find((run) => run.id === runId);
  const running = runs.some((run) => run.status === 'running');
  const harvesting = ACTIVE.includes(status?.harvest_job?.status);
  const start = useMutation({ mutationFn: () => api.planner.run(config), onSuccess: ({ data }) => { setRunId(data.id); onChange(); } });
  const exportRun = useMutation({ mutationFn: () => api.planner.exportRun(runId) });
  const set = (key, value) => setConfig((old) => ({ ...old, [key]: value }));
  const list = (key) => (config?.[key] || []).join('\n');
  const setList = (key, text) => set(key, text.split(/[\n,]/).map((item) => item.trim().replace(/ /g, '_')).filter(Boolean));
  return <section className="rounded border border-slate-800 p-4 space-y-3">
    <h2 className="font-semibold">3. Plan</h2>
    <p className="text-xs text-slate-400">Each artist picks images in turns by quality (within the artist), new content, character need, tag rarity and pose/angle bonus. Characters are counted across all artists, so a needed character can come from anyone who draws it. Re-run after changing settings or reviewing; earlier runs are kept.</p>
    {config && <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
      {NUMBER_FIELDS.map(([key, label]) => <label key={key} className="flex flex-col gap-1"><span className="text-slate-400">{label}</span>
        <input type="number" step="any" className={field} value={config[key] ?? ''} onChange={(event) => set(key, event.target.value === '' ? null : Number(event.target.value))} /></label>)}
      <label className="flex items-center gap-2"><input type="checkbox" checked={config.exclude_multi_artist} onChange={(event) => set('exclude_multi_artist', event.target.checked)} />Skip posts credited to several artists</label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={config.character_topup} onChange={(event) => set('character_topup', event.target.checked)} />Top up characters below their target</label>
      <label className="flex flex-col gap-1"><span className="text-slate-400">Ratings (blank = all)</span>
        <input className={field} value={(config.allowed_ratings || []).join(', ')} placeholder="general, sensitive, safe, questionable, explicit"
          onChange={(event) => { const values = event.target.value.split(',').map((item) => item.trim()).filter(Boolean); set('allowed_ratings', values.length ? values : null); }} /></label>
    </div>}
    {config && <details className="text-sm"><summary className="cursor-pointer text-slate-300">Score weights and tag lists</summary>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mt-3">{WEIGHT_FIELDS.map((key) => <label key={key} className="flex flex-col gap-1"><span className="text-slate-400">{key.replace('weight_', '')}</span>
        <input type="number" step="0.1" min={0} className={field} value={config[key]} onChange={(event) => set(key, Number(event.target.value))} /></label>)}</div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
        <label className="flex flex-col gap-1"><span className="text-slate-400">Blocked tags (post is skipped)</span><textarea className={`${field} h-36`} value={list('blocked_tags')} onChange={(event) => setList('blocked_tags', event.target.value)} /></label>
        <label className="flex flex-col gap-1"><span className="text-slate-400">Boost tags (rare angles and poses)</span><textarea className={`${field} h-36`} value={list('boost_tags')} onChange={(event) => setList('boost_tags', event.target.value)} /></label>
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
      {selected?.status === 'completed' && <button className={quiet} onClick={() => setConfig(selected.config)}>Load this run’s settings</button>}
    </div>
    {[start.error, exportRun.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {exportRun.data && <p className="text-sm text-green-300">Exported to {exportRun.data.data.path}: {exportRun.data.data.files.join(', ')}</p>}
    {selected?.error && <p className="text-red-400 text-sm">{selected.error}</p>}
    {selected?.summary && <RunSummary summary={selected.summary} />}
  </section>;
}

function RunSummary({ summary }) {
  return <div className="space-y-3 text-sm">
    <p>{number(summary.artists_kept)} artists · {number(summary.images)} images · {number(summary.samples_per_pass)} training samples per pass · planned in {summary.seconds}s</p>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {Object.entries(summary.families || {}).map(([family, data]) => <div key={family} className="rounded border border-slate-800 p-3 space-y-1">
        <p className="font-semibold">{family === 'danbooru' ? 'Danbooru + Gelbooru' : 'e621'}</p>
        <p>{number(data.artists_kept)} artists kept{Object.entries(data.artists_dropped || {}).map(([reason, count]) => ` · ${number(count)} dropped (${reason.replace(/_/g, ' ')})`).join('')}</p>
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
  const [site, setSite] = useState('');
  const [runStatus, setRunStatus] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [artistId, setArtistId] = useState(null);
  const params = { run_id: runId, site: site || undefined, run_status: runStatus || undefined, q: search || undefined, offset, limit: 50 };
  const artists = useQuery({ queryKey: ['planner-artists', params], queryFn: async () => (await api.planner.artists(params)).data, placeholderData: (old) => old });
  useEffect(() => setOffset(0), [site, runStatus, search, runId]);
  return <section className="rounded border border-slate-800 p-4 space-y-3">
    <h2 className="font-semibold">4. Review run #{runId}</h2>
    <p className="text-xs text-slate-400">Lock an image to always include it, or ban it to never include it. Locks and bans apply from the next plan run. Thumbnails are fetched by the backend and cached in the planner folder.</p>
    <div className="flex flex-wrap gap-2">
      <input className={field} placeholder="Search artists" value={search} onChange={(event) => setSearch(event.target.value)} />
      <select className={field} value={site} onChange={(event) => setSite(event.target.value)}><option value="">All sites</option>{SITES.map((item) => <option key={item}>{item}</option>)}</select>
      <select className={field} value={runStatus} onChange={(event) => setRunStatus(event.target.value)}><option value="">Kept and dropped</option><option value="kept">Kept</option><option value="dropped">Dropped</option></select>
    </div>
    {artists.error && <p role="alert" className="text-red-400">{errorText(artists.error)}</p>}
    <div className="grid grid-cols-1 md:grid-cols-[18rem_1fr] gap-4">
      <div className="space-y-2">
        <div className="max-h-[36rem] overflow-auto divide-y divide-slate-800 text-sm">
          {artists.data?.items.map((item) => <button key={item.id} onClick={() => setArtistId(item.id)} className={`block w-full text-left py-1.5 px-1 ${item.id === artistId ? 'text-blue-300' : ''}`}>
            {item.display_name} <span className="text-xs text-slate-500">{item.site}</span>
            <span className="block text-xs text-slate-400">{item.run_status === 'kept' ? `${item.selected} images × ${item.repeats}` : item.run_status === 'dropped' ? `dropped: ${String(item.run_reason).replace(/_/g, ' ')} (${item.usable} usable)` : `${item.harvest_status}, not in this run`}</span>
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
      return <div key={`${item.site}-${item.remote_id}`} className={`rounded border p-1 text-xs space-y-1 ${item.override === 'lock' ? 'border-green-600' : item.override === 'ban' ? 'border-red-700 opacity-60' : 'border-slate-800'}`}>
        <a href={url} target="_blank" rel="noreferrer" title={`${item.characters || 'no character tags'}\n${item.width}×${item.height} · ${item.rating}\n${reasons}`}>
          <img src={backendAssetUrl(`/api/planner/thumbs/${artistId}/${item.site}/${item.remote_id}`)} alt="" loading="lazy" className="w-full h-28 object-contain bg-black/30" />
        </a>
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
