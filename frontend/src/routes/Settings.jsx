import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
import ServerSettings from '../components/ServerSettings';
import ProcessingSettings from '../components/ProcessingSettings';

function Settings() {
  const queryClient = useQueryClient();
  const [userId, setUserId] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [groundTruthCategories, setGroundTruthCategories] = useState([]);
  const [parallelWorkers, setParallelWorkers] = useState(2);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleMinutes, setScheduleMinutes] = useState(1440);
  const [scheduleLimit, setScheduleLimit] = useState(20);
  const [pixivToken, setPixivToken] = useState('');
  const [twitterBrowser, setTwitterBrowser] = useState('firefox');
  const [twitterProfile, setTwitterProfile] = useState('');
  const [twitterCookieFile, setTwitterCookieFile] = useState(null);

  const { data: config } = useQuery({
    queryKey: ['gelbooru-config'],
    queryFn: async () => (await api.providers.gelbooruConfig()).data,
  });
  const { data: status, refetch: testGelbooru, isFetching: testing } = useQuery({
    queryKey: ['provider-status', 'gelbooru'],
    queryFn: async () => (await api.providers.status('gelbooru')).data,
    enabled: false,
  });
  const { data: decoder } = useQuery({
    queryKey: ['media-decoder-status'],
    queryFn: async () => (await api.images.decoderStatus()).data,
  });
  const { data: categoryPolicy } = useQuery({
    queryKey: ['global-tag-category-policy'],
    queryFn: async () => (await api.tags.getGlobalCategoryPolicy()).data,
  });
  const { data: parallelism } = useQuery({
    queryKey: ['parallelism-settings'],
    queryFn: async () => (await api.settings.getParallelism()).data,
  });
  const { data: schedule } = useQuery({
    queryKey: ['sync-schedule'],
    queryFn: async () => (await api.sync.getSchedule()).data,
  });
  const { data: galleryConfig } = useQuery({
    queryKey: ['gallery-dl-config'],
    queryFn: async () => (await api.providers.galleryDlConfig()).data,
  });
  const { data: providers } = useQuery({
    queryKey: ['providers'],
    queryFn: async () => (await api.providers.list()).data,
  });

  useEffect(() => { if (config?.user_id) setUserId(config.user_id); }, [config?.user_id]);
  useEffect(() => { if (categoryPolicy) setGroundTruthCategories(categoryPolicy.categories); }, [categoryPolicy]);
  useEffect(() => { if (parallelism?.workers) setParallelWorkers(parallelism.workers); }, [parallelism?.workers]);
  useEffect(() => {
    if (!schedule) return;
    setScheduleEnabled(schedule.enabled);
    setScheduleMinutes(schedule.interval_minutes);
    setScheduleLimit(schedule.limit_per_source);
  }, [schedule]);
  useEffect(() => {
    if (!galleryConfig?.twitter) return;
    setTwitterBrowser(galleryConfig.twitter.browser || 'firefox');
    setTwitterProfile(galleryConfig.twitter.profile || '');
  }, [galleryConfig]);

  const saveMutation = useMutation({
    mutationFn: () => api.providers.saveGelbooruConfig({ user_id: userId.trim(), api_key: apiKey.trim() }),
    onSuccess: async () => {
      setApiKey('');
      await queryClient.invalidateQueries({ queryKey: ['gelbooru-config'] });
      await testGelbooru();
    },
  });
  const categoryMutation = useMutation({
    mutationFn: (categories) => api.tags.updateGlobalCategoryPolicy(categories),
    onSuccess: (response) => {
      setGroundTruthCategories(response.data.categories);
      queryClient.invalidateQueries({ queryKey: ['global-tag-category-policy'] });
      queryClient.invalidateQueries({ queryKey: ['collection-tags'] });
    },
  });
  const parallelismMutation = useMutation({
    mutationFn: () => api.settings.updateParallelism(parallelWorkers),
    onSuccess: (response) => {
      setParallelWorkers(response.data.workers);
      queryClient.setQueryData(['parallelism-settings'], response.data);
    },
  });
  const scheduleMutation = useMutation({
    mutationFn: () => api.sync.updateSchedule({ enabled: scheduleEnabled, interval_minutes: Number(scheduleMinutes), limit_per_source: Number(scheduleLimit), sort: 'latest' }),
    onSuccess: (response) => queryClient.setQueryData(['sync-schedule'], response.data),
  });
  const runScheduleMutation = useMutation({
    mutationFn: () => api.sync.runScheduleNow(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sync-history'] });
      queryClient.invalidateQueries({ queryKey: ['sync-history-brief'] });
    },
  });
  const pixivMutation = useMutation({
    mutationFn: () => api.providers.savePixivConfig({ refresh_token: pixivToken.trim() }),
    onSuccess: () => {
      setPixivToken('');
      queryClient.invalidateQueries({ queryKey: ['gallery-dl-config'] });
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
  const twitterMutation = useMutation({
    mutationFn: () => api.providers.saveTwitterConfig({ browser: twitterBrowser, profile: twitterProfile.trim() }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gallery-dl-config'] });
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
  const twitterCookieMutation = useMutation({
    mutationFn: () => api.providers.uploadTwitterCookies(twitterCookieFile),
    onSuccess: () => {
      setTwitterCookieFile(null);
      queryClient.invalidateQueries({ queryKey: ['gallery-dl-config'] });
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
  const removeTwitterCookieMutation = useMutation({
    mutationFn: () => api.providers.removeTwitterCookies(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gallery-dl-config'] });
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
  const toggleCategory = (category) => {
    const next = groundTruthCategories.includes(category)
      ? groundTruthCategories.filter((item) => item !== category)
      : [...groundTruthCategories, category];
    setGroundTruthCategories(next);
    categoryMutation.mutate(next);
  };

  return (
    <div className="max-w-3xl mx-auto space-y-4 text-slate-300">
      <h1 className="text-xl font-bold text-slate-100">Settings</h1>
      <ServerSettings />
      <ProcessingSettings />

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <h2 className="font-semibold text-slate-100">Parallel downloads and processing</h2>
        <p className="mt-1 text-xs text-slate-400">Controls how many posts a sync or Import batch downloads, decodes, and writes at once. New jobs use the saved value.</p>
        <div className="mt-4 flex flex-wrap items-end gap-3">
          <label className="text-xs text-slate-400">Workers
            <input aria-label="Parallel workers" type="number" min="1" step="1" value={parallelWorkers} onChange={event => setParallelWorkers(Number(event.target.value))} className="mt-1 block w-44 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm text-slate-200" />
          </label>
          <button type="button" onClick={() => parallelismMutation.mutate()} disabled={parallelismMutation.isPending || !Number.isSafeInteger(parallelWorkers) || parallelWorkers < 1 || parallelWorkers === parallelism?.workers} className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">
            {parallelismMutation.isPending ? 'Saving...' : 'Save worker count'}
          </button>
        </div>
        <p className="mt-3 text-xs text-slate-400">Enter any positive worker count; there is no application maximum. Two remains the default. More workers can increase CPU/RAM use; provider request-rate limits still apply.</p>
        {parallelismMutation.isSuccess && <p className="mt-2 text-xs text-emerald-400">Saved. The next job will use {parallelWorkers} worker{parallelWorkers === 1 ? '' : 's'}.</p>}
        {parallelismMutation.isError && <p className="mt-2 text-xs text-red-400">{typeof parallelismMutation.error.response?.data?.detail === 'string' ? parallelismMutation.error.response.data.detail : parallelismMutation.error.message}</p>}
      </section>

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-semibold text-slate-100">Scheduled Sync All</h2>
            <p className="mt-1 text-xs text-slate-400">Runs every enabled provider in every enabled collection. Jobs, progress, and results are retained in sync history.</p>
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-300"><input type="checkbox" checked={scheduleEnabled} onChange={(event) => setScheduleEnabled(event.target.checked)} /> Enabled</label>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-slate-400">Every (minutes)
            <input type="number" min="5" max="10080" value={scheduleMinutes} onChange={(event) => setScheduleMinutes(event.target.value)} className="mt-1 block w-full rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm" />
          </label>
          <label className="text-xs text-slate-400">Posts per source
            <input type="number" min="1" max="320" value={scheduleLimit} onChange={(event) => setScheduleLimit(event.target.value)} className="mt-1 block w-full rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm" />
          </label>
        </div>
        <p className="mt-3 text-xs text-slate-400">Scheduled runs always check new posts first, then use remaining capacity to continue the older-catalog backfill.</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => scheduleMutation.mutate()} disabled={scheduleMutation.isPending} className="rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-50">{scheduleMutation.isPending ? 'Saving...' : 'Save schedule'}</button>
          <button type="button" onClick={() => runScheduleMutation.mutate()} disabled={runScheduleMutation.isPending} className="rounded border border-[#202a34] px-3 py-1.5 text-xs disabled:opacity-50">{runScheduleMutation.isPending ? 'Starting...' : 'Run configured sync now'}</button>
          {schedule?.next_run_at && scheduleEnabled && <span className="text-xs text-slate-400">Next: {new Date(schedule.next_run_at).toLocaleString()}</span>}
        </div>
        {scheduleMutation.isSuccess && <p className="mt-2 text-xs text-emerald-400">Schedule saved.</p>}
        {(scheduleMutation.isError || runScheduleMutation.isError) && <p className="mt-2 text-xs text-red-400">{(scheduleMutation.error || runScheduleMutation.error)?.response?.data?.detail || (scheduleMutation.error || runScheduleMutation.error)?.message}</p>}
      </section>

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <h2 className="font-semibold text-slate-100">Default ground-truth sections</h2>
        <p className="mt-1 text-xs text-slate-400">Controls trainer-facing sidecars globally. Individual folders can inherit or override this. Imported provider metadata is always preserved.</p>
        <div className="mt-3 flex flex-wrap gap-3">
          {(categoryPolicy?.available_categories || ['artist', 'character', 'copyright', 'species', 'general', 'meta']).map((category) => (
            <label key={category} className="flex items-center gap-2 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-xs capitalize">
              <input type="checkbox" checked={groundTruthCategories.includes(category)} onChange={() => toggleCategory(category)} disabled={categoryMutation.isPending} />
              {category === 'meta' ? 'metadata' : category}
            </label>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-400">Changes immediately update sidecars in folders using the global defaults. Metadata is excluded by default.</p>
        {categoryMutation.isError && <p className="mt-2 text-xs text-red-400">{categoryMutation.error.response?.data?.detail || categoryMutation.error.message}</p>}
      </section>

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold text-slate-100">Gelbooru API</h2>
            <p className="mt-1 text-xs text-slate-400">Gelbooru currently requires account API credentials for DAPI searches.</p>
          </div>
          <span className={`text-xs ${config?.configured ? 'text-emerald-400' : 'text-amber-400'}`}>
            {config?.configured ? 'Credentials saved' : 'Setup required'}
          </span>
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-slate-400">User ID
            <input value={userId} onChange={(event) => setUserId(event.target.value)} inputMode="numeric" placeholder="Numeric Gelbooru user ID" className="mt-1 block w-full rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm text-slate-200" />
          </label>
          <label className="text-xs text-slate-400">API key
            <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={config?.api_key_configured ? 'Enter a new key to replace saved key' : 'Gelbooru API key'} className="mt-1 block w-full rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm text-slate-200" />
          </label>
        </div>

        <p className="mt-3 text-xs text-slate-400">Log in to Gelbooru, open Account → Options, then copy your numeric User ID and API key. The key is stored locally and is never returned by the API or shown in logs.</p>
        <div className="mt-3 flex gap-2">
          <button type="button" onClick={() => saveMutation.mutate()} disabled={!userId.trim() || !apiKey.trim() || saveMutation.isPending} className="rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-50">{saveMutation.isPending ? 'Saving...' : 'Save and test'}</button>
          <button type="button" onClick={() => testGelbooru()} disabled={!config?.configured || testing} className="rounded border border-[#202a34] px-3 py-1.5 text-xs text-slate-300 disabled:opacity-50">{testing ? 'Testing...' : 'Test saved credentials'}</button>
        </div>
        {saveMutation.isError && <p className="mt-3 text-xs text-red-400">{saveMutation.error.response?.data?.detail || saveMutation.error.message}</p>}
        {status && <p className={`mt-3 text-xs ${status.available ? 'text-emerald-400' : 'text-red-400'}`}>{status.available ? 'Gelbooru connection successful.' : status.error}</p>}
      </section>

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-semibold text-slate-100">Pixiv, Twitter / X, ArtStation, and Pawchive</h2>
            <a href="https://github.com/RicemanT/Illustration-Scrapping-Studio/blob/main/docs/PROVIDER_SETUP.md" target="_blank" rel="noopener noreferrer" className="text-xs text-blue-300 underline">Step-by-step provider setup guide</a>
            <p className="mt-1 text-xs text-slate-400">Powered by gallery-dl {galleryConfig?.version || ''}. Originals and all assets in multi-image posts are imported; provider thumbnails are never substituted for failed video or archive media.</p>
          </div>
          <span className={`text-xs ${galleryConfig?.installed ? 'text-emerald-400' : 'text-red-400'}`}>{galleryConfig?.installed ? 'Runtime ready' : 'gallery-dl missing'}</span>
        </div>

        <div className="mt-4 border-t border-[#202a34] pt-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-200">Pixiv OAuth</h3>
            <span className={`text-xs ${galleryConfig?.pixiv?.configured ? 'text-emerald-400' : 'text-amber-400'}`}>{galleryConfig?.pixiv?.configured ? 'Token saved' : 'Setup required'}</span>
          </div>
          <div className="mt-2 space-y-1 text-xs text-slate-400">
            <p>1. Open a terminal in the project directory. Windows: <code className="text-blue-300">.venv\Scripts\gallery-dl.exe oauth:pixiv</code>. Jupyter/Linux: <code className="text-blue-300">.venv/bin/gallery-dl oauth:pixiv</code>. Use a terminal rather than a notebook cell because this command needs interactive input.</p>
            <p>If running on Jupyter, open the printed login URL in your laptop browser, then paste the requested code back into the Jupyter terminal. You can also complete OAuth on your laptop and save the final token in this server's Settings.</p>
            <p>2. When the terminal shows <code className="text-blue-300">code:</code>, OAuth is not finished yet. Open its login URL, follow the displayed F12/Network instructions, and paste the callback's short-lived <code className="text-blue-300">code</code> into the terminal.</p>
            <p>3. After gallery-dl exchanges that code, it prints a <code className="text-blue-300">refresh-token</code>. Paste that final refresh token below—not the login URL or callback code. It is stored locally and never returned by the API.</p>
          </div>
          <div className="mt-3 flex gap-2">
            <input type="password" value={pixivToken} onChange={(event) => setPixivToken(event.target.value)} placeholder={galleryConfig?.pixiv?.configured ? 'Replace the saved refresh token' : 'Final Pixiv refresh-token value'} className="min-w-0 flex-1 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm" />
            <button type="button" onClick={() => pixivMutation.mutate()} disabled={!pixivToken.trim() || pixivMutation.isPending} className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">{pixivMutation.isPending ? 'Saving...' : 'Save token'}</button>
          </div>
          {pixivMutation.isError && <p className="mt-2 text-xs text-red-400">{pixivMutation.error.response?.data?.detail || pixivMutation.error.message}</p>}
        </div>

        <div className="mt-4 border-t border-[#202a34] pt-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-200">Twitter / X authentication</h3>
            <span className={`text-xs ${galleryConfig?.twitter?.configured ? 'text-emerald-400' : 'text-amber-400'}`}>{galleryConfig?.twitter?.configured ? (galleryConfig.twitter.auth_mode === 'cookies_file' ? 'Cookie file active' : 'Browser selected') : 'Setup required'}</span>
          </div>
          <p className="mt-1 text-xs text-slate-400">On your laptop, sign in to x.com and export that site's cookies in Netscape cookies.txt format using a trusted tool. The export must include auth_token and ct0. Choose the file below, then click Upload cookies.txt. It is stored privately on the backend that performs the sync, so your browser can stay open and the same workflow works locally or on a Jupyter server.</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input type="file" accept=".txt,text/plain" onChange={(event) => setTwitterCookieFile(event.target.files?.[0] || null)} className="min-w-0 flex-1 rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-xs" />
            <button type="button" onClick={() => twitterCookieMutation.mutate()} disabled={!twitterCookieFile || twitterCookieMutation.isPending} className="rounded bg-[#344a73] px-3 py-2 text-xs text-white disabled:opacity-50">{twitterCookieMutation.isPending ? 'Uploading...' : 'Upload cookies.txt'}</button>
            {galleryConfig?.twitter?.cookie_file_configured && <button type="button" onClick={() => removeTwitterCookieMutation.mutate()} disabled={removeTwitterCookieMutation.isPending} className="rounded border border-red-800 px-3 py-2 text-xs text-red-300 disabled:opacity-50">Remove cookie file</button>}
          </div>
          {twitterCookieMutation.isSuccess && <p className="mt-2 text-xs text-emerald-400">Validated and saved. Cookie values are never returned by the API.</p>}
          {(twitterCookieMutation.isError || removeTwitterCookieMutation.isError) && <p className="mt-2 text-xs text-red-400">{(twitterCookieMutation.error || removeTwitterCookieMutation.error)?.response?.data?.detail || (twitterCookieMutation.error || removeTwitterCookieMutation.error)?.message}</p>}
          <p className="mt-4 border-t border-[#202a34] pt-3 text-xs text-slate-400">Browser mode reads a browser on the backend machine, not your laptop when using Jupyter. After uploading cookies, leave cookie-file mode active. Browser-profile extraction remains available as a local fallback. Chromium browsers may need to be fully closed, so saving a browser below switches Twitter back to browser mode.</p>
          <div className="mt-3 grid gap-2 sm:grid-cols-[12rem_1fr_auto]">
            <select value={twitterBrowser} onChange={(event) => setTwitterBrowser(event.target.value)} className="rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm">
              {(galleryConfig?.twitter_browsers || ['firefox', 'chrome', 'edge']).map((browser) => <option key={browser} value={browser}>{browser}</option>)}
            </select>
            <input value={twitterProfile} onChange={(event) => setTwitterProfile(event.target.value)} placeholder="Profile name/path (optional)" className="rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm" />
            <button type="button" onClick={() => twitterMutation.mutate()} disabled={twitterMutation.isPending} className="rounded border border-[#202a34] px-3 py-2 text-xs text-slate-200 disabled:opacity-50">{twitterMutation.isPending ? 'Saving...' : 'Use browser mode'}</button>
          </div>
          {twitterMutation.isError && <p className="mt-2 text-xs text-red-400">{twitterMutation.error.response?.data?.detail || twitterMutation.error.message}</p>}
        </div>

        <div className="mt-4 border-t border-[#202a34] pt-4 text-xs text-slate-400">
          {['artstation', 'pawchive'].map((name) => {
            const descriptor = providers?.items?.find((item) => item.name === name);
            return <p key={name} className="mt-1"><span className="capitalize text-slate-200">{name}</span>: <span className={descriptor?.available ? 'text-emerald-400' : 'text-amber-400'}>{descriptor?.available ? 'ready' : descriptor?.unavailable_reason || 'checking'}</span>{name === 'pawchive' ? ' (use a full creator/post URL as the folder query)' : ''}</p>;
          })}
        </div>
      </section>

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold text-slate-100">Training image processing</h2>
            <p className="mt-1 text-xs text-slate-400">Import processing is configurable above. By default, the longest side is capped at 2000px using INTER_AREA, with JPEG or lossless WebP output. Original-media frame extraction remains available when post-processing is disabled.</p>
          </div>
          <span className={`text-xs ${decoder?.available ? 'text-emerald-400' : 'text-amber-400'}`}>
            {decoder?.available ? 'Video ready' : 'Video unavailable'}
          </span>
        </div>
        {!decoder?.available && <p className="mt-3 text-xs text-amber-400">Video posts are skipped unless a full-resolution frame can be decoded from the original. Provider thumbnails are never imported for videos.</p>}
        {decoder?.available && <p className="mt-3 text-xs text-slate-400">Backend: {decoder.backend}</p>}
        <p className="mt-2 text-xs text-slate-400">Archives: ZIP {decoder?.archives?.zip?.available ? 'ready' : 'unavailable'} ({decoder?.archives?.zip?.backend || 'no decoder'}); RAR {decoder?.archives?.rar?.available ? 'ready' : 'unavailable'} ({decoder?.archives?.rar?.backend || 'no decoder'}). Archive thumbnails are never used as fallbacks.</p>
      </section>

      <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
        <h2 className="font-semibold text-slate-100">Query syntax</h2>
        <div className="mt-2 space-y-1 text-sm text-slate-400">
          <p>Use normal spaces in artist and character names. The app translates them for each provider.</p>
          <p><code className="text-blue-300">rating:safe</code> filters ratings; <code className="text-blue-300">-tag</code> excludes a tag.</p>
          <p>You never need to type underscores.</p>
        </div>
      </section>
    </div>
  );
}

export default Settings;
