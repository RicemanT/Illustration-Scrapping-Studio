import React from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import api from '../api/client';

const quiet = 'rounded border border-slate-700 px-3 py-2 text-sm disabled:opacity-40';
const number = (value) => Number(value || 0).toLocaleString();
const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : String(error?.message || 'Request failed');
};

// Section 8: character facts for the captioning script (who, which series, usual look), built from the harvest.
export default function CaptionFactsPanel() {
  const status = useQuery({ queryKey: ['caption-facts'], queryFn: async () => (await api.planner.captionFacts()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false) });
  const start = useMutation({ mutationFn: api.planner.buildCaptionFacts, onSuccess: () => status.refetch() });
  const data = status.data;
  const result = data?.result;
  const running = data?.status === 'running';
  return (
    <section className="rounded border border-slate-800 p-4 space-y-3">
      <h2 className="font-semibold">8. Captioning</h2>
      <p className="text-xs text-slate-400 max-w-3xl">
        Captioning models know popular characters but guess at rarer ones. <b>Build character facts</b> scans every harvested post and records,
        for each character tag, a clean name, its series (copyrights on at least a quarter of its posts) and its usual look (hair, eyes, ears, tails,
        horns, halos, species … on at least 40% of the posts showing it alone). Your captioning script reads
        the file, finds the character tags in each image's <code>.txt</code> and passes their facts with the tags, so no web search is needed.
        Rebuild after harvesting more artists.
      </p>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <button className={quiet} disabled={running || start.isPending} onClick={() => start.mutate()}>
          {running ? 'Building…' : result ? 'Rebuild character facts' : 'Build character facts'}</button>
        {running && <span className="text-xs text-blue-200">Scanning posts{data.done ? `: ${number(data.done)}${data.total ? ` of up to ${number(data.total)}` : ''}` : '…'}</span>}
      </div>
      {start.isError && <p role="alert" className="text-sm text-red-400">{errorText(start.error)}</p>}
      {data?.status === 'failed' && <p role="alert" className="text-sm text-red-400">{data.error}</p>}
      {result && <p className="text-xs text-slate-300">
        {number(result.characters?.danbooru)} Danbooru/Gelbooru and {number(result.characters?.e621)} e621 characters
        {result.with_series != null ? ` · ${number(result.with_series)} with a series · ${number(result.with_appearance)} with a usual look` : ''}
        {' '}· built {result.built_at ? new Date(result.built_at).toLocaleString() : '—'} · <code className="select-all">{result.path}</code>
      </p>}
    </section>
  );
}
