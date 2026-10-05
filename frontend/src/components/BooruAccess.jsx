import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const field = 'mt-1 block w-full rounded border border-[#202a34] bg-[#090d12] px-3 py-2 text-sm text-slate-200';
const section = 'bg-[#0c1219] border border-[#202a34] rounded p-4';
const primary = 'rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-50';
const quiet = 'rounded border border-[#202a34] px-3 py-1.5 text-xs text-slate-300 disabled:opacity-50';
const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : String(error?.message || 'Request failed');
};

const ACCOUNTS = {
  danbooru: {
    title: 'Danbooru account (optional)',
    help: 'Danbooru allows the same number of requests with or without an account (10 per second per IP at most, about 1 per second for long sessions). With an account, requests carry your user ID, which is how Danbooru asks bots to identify themselves. Create an API key on your Danbooru profile page.',
    login: 'Danbooru username',
  },
  e621: {
    title: 'e621 account (optional)',
    help: 'e621 hides the file link of some posts from visitors who are not logged in (about 8% in a test harvest); an account may unlock them. Requests then name your e621 username, as e621 asks. Create an API key under Account → Manage API Access.',
    login: 'e621 username',
  },
};

function Account({ site }) {
  const client = useQueryClient();
  const info = ACCOUNTS[site];
  const [login, setLogin] = useState('');
  const [apiKey, setApiKey] = useState('');
  const account = useQuery({ queryKey: ['booru-account', site], queryFn: async () => (await api.providers.account(site)).data });
  useEffect(() => { if (account.data?.login) setLogin(account.data.login); }, [account.data?.login]);
  const refresh = () => { client.invalidateQueries({ queryKey: ['booru-account', site] }); client.invalidateQueries({ queryKey: ['providers'] }); };
  const save = useMutation({ mutationFn: () => api.providers.saveAccount(site, { login: login.trim(), api_key: apiKey.trim() }), onSuccess: () => { setApiKey(''); refresh(); } });
  const remove = useMutation({ mutationFn: () => api.providers.removeAccount(site), onSuccess: () => { setLogin(''); setApiKey(''); refresh(); } });
  return <div className="space-y-2">
    <h3 className="text-sm font-semibold text-slate-100">{info.title}</h3>
    <p className="text-xs text-slate-400">{info.help}</p>
    <div className="grid gap-3 md:grid-cols-2">
      <label className="text-xs text-slate-400">{info.login}<input value={login} onChange={(event) => setLogin(event.target.value)} autoComplete="off" className={field} /></label>
      <label className="text-xs text-slate-400">API key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off"
        placeholder={account.data?.configured ? 'Enter a new key to replace the saved key' : 'API key'} className={field} /></label>
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <button type="button" className={primary} disabled={save.isPending || !login.trim() || !apiKey.trim()} onClick={() => save.mutate()}>{save.isPending ? 'Checking…' : 'Check and save'}</button>
      {account.data?.configured && <button type="button" className={quiet} disabled={remove.isPending} onClick={() => remove.mutate()}>Remove account</button>}
      {account.data?.configured && <span className="text-xs text-emerald-400">Saved: {account.data.login}{account.data.user_id ? ` (user #${account.data.user_id})` : ''}</span>}
    </div>
    {save.isError && <p className="text-xs text-red-400">{errorText(save.error)}</p>}
    {remove.isError && <p className="text-xs text-red-400">{errorText(remove.error)}</p>}
  </div>;
}

const SITES = [
  ['danbooru', 'Danbooru', 'Hard limit 10 requests/s per IP; about 1/s asked for long sessions.'],
  ['gelbooru', 'Gelbooru', 'No published limit; it throttles heavy use.'],
  ['e621', 'e621', 'Hard limit 2 requests/s (faster gets HTTP 503); 1/s asked for sustained use.'],
];
const FAST = { danbooru: { api: 0.5, download: 0.05 }, gelbooru: { api: 0.5, download: 0.05 }, e621: { api: 0.75, download: 0.05 } };
const rate = (seconds) => seconds > 0 ? `${(1 / seconds).toFixed(seconds < 0.2 ? 0 : 1)}/s` : 'no limit';

function Pace() {
  const client = useQueryClient();
  const pacing = useQuery({ queryKey: ['booru-pacing'], queryFn: async () => (await api.providers.pacing()).data });
  const [values, setValues] = useState(null);
  useEffect(() => { if (pacing.data && !values) setValues(pacing.data.settings); }, [pacing.data, values]);
  const save = useMutation({ mutationFn: (next) => api.providers.savePacing(next), onSuccess: ({ data }) => { setValues(data.settings); client.setQueryData(['booru-pacing'], data); } });
  if (!values || !pacing.data) return <p className="text-xs text-slate-400">Loading request pace…</p>;
  const set = (site, kind, value) => setValues((old) => ({ ...old, [site]: { ...old[site], [kind]: value } }));
  const changed = JSON.stringify(values) !== JSON.stringify(pacing.data.settings);
  return <div className="space-y-3">
    <h3 className="text-sm font-semibold text-slate-100">Request pace</h3>
    <p className="text-xs text-slate-400">Seconds between request starts, shared by every job on the same site. API requests (searches and post lookups) are what the sites rate-limit. Image files come from the sites' file servers, which tools such as gallery-dl do not throttle; downloads also run in parallel up to the worker count above. If a site answers "too many requests", the app waits as asked, slows that site down and recovers gradually. Changes apply to running jobs within a second.</p>
    <div className="overflow-x-auto"><table className="text-xs w-full min-w-[34rem]">
      <thead><tr className="text-slate-400 text-left"><th className="py-1 pr-3 font-medium">Site</th><th className="py-1 pr-3 font-medium">API requests (s)</th><th className="py-1 pr-3 font-medium">File downloads (s)</th><th className="py-1 font-medium">Documented limit</th></tr></thead>
      <tbody>{SITES.map(([site, label, limit]) => <tr key={site} className="border-t border-[#202a34] align-top">
        <td className="py-2 pr-3 text-slate-200">{label}</td>
        {['api', 'download'].map((kind) => <td key={kind} className="py-2 pr-3">
          <input aria-label={`${label} ${kind} interval`} type="number" step="0.05" min={pacing.data.floors[site][kind]} value={values[site][kind]}
            onChange={(event) => set(site, kind, event.target.value === '' ? '' : Number(event.target.value))}
            className="w-24 rounded border border-[#202a34] bg-[#090d12] px-2 py-1 text-sm text-slate-200" />
          <span className="ml-2 text-slate-500">{rate(Number(values[site][kind]))} · min {pacing.data.floors[site][kind]}</span>
        </td>)}
        <td className="py-2 text-slate-400">{limit}</td>
      </tr>)}</tbody>
    </table></div>
    <div className="flex flex-wrap gap-2">
      <button type="button" className={primary} disabled={save.isPending || !changed} onClick={() => save.mutate(values)}>{save.isPending ? 'Saving…' : 'Save pace'}</button>
      <button type="button" className={quiet} disabled={save.isPending} onClick={() => save.mutate(pacing.data.defaults)}>Polite (defaults)</button>
      <button type="button" className={quiet} disabled={save.isPending} onClick={() => save.mutate(FAST)}>Fast</button>
    </div>
    <p className="text-xs text-slate-500">Fast: Danbooru and Gelbooru 2 API requests/s, e621 1.3/s, about 20 file downloads/s each. Values below each site's minimum are raised to it when saved.</p>
    {save.isError && <p className="text-xs text-red-400">{errorText(save.error)}</p>}
  </div>;
}

export default function BooruAccess() {
  return <section className={`${section} space-y-5`}>
    <div>
      <h2 className="font-semibold text-slate-100">Danbooru and e621 access, request pace</h2>
      <p className="mt-1 text-xs text-slate-400">Keys are stored on the server running the app and are never returned by the API or shown in logs.</p>
    </div>
    <Account site="danbooru" />
    <Account site="e621" />
    <Pace />
  </section>;
}
