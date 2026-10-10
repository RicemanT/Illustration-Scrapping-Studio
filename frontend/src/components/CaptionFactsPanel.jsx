import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
import FlaggedCaptionViewer from './FlaggedCaptionViewer';

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
      <MachineTags />
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
      <CaptionCheck />
    </section>
  );
}

// Tagger notebook results (DBv4 for Danbooru/Gelbooru, Hydra for e621) imported as extra general tags.
function MachineTags() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ['machine-tags'], queryFn: async () => (await api.planner.machineTags()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false) });
  const start = useMutation({ mutationFn: (body) => api.planner.startMachineTags(body),
    onSuccess: () => { status.refetch(); queryClient.invalidateQueries({ queryKey: ['image'] }); } });
  const data = status.data;
  const running = data?.status === 'running';
  const summary = data?.summary;
  const models = Object.entries(summary?.models || {});
  const waiting = (summary?.files || []).filter((file) => file.new);
  const result = data?.result;
  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <h3 className="text-sm font-semibold text-slate-200">Tagger tags</h3>
      <p className="text-xs text-slate-400 max-w-3xl">
        The tagger notebooks (DBv4 for Danbooru/Gelbooru, Hydra for e621) write their results to <code className="select-all">{summary?.folder || 'planner/tagger'}</code>.
        <b> Import</b> adds the predicted general tags after each image's own booru tags in its <code>.txt</code> (tags the post already has are not
        repeated; a tag you remove in the tag editor stays removed). Import before captioning so the captions see them.
      </p>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <button className={quiet} disabled={running || start.isPending} onClick={() => start.mutate({ action: 'import' })}>
          {running && data?.action === 'import' ? 'Importing…' : `Import tagger results${waiting.length ? ` (${waiting.length} new file${waiting.length > 1 ? 's' : ''})` : ''}`}</button>
        {running && <span className="text-xs text-blue-200">{data.stage}{data.done ? `: ${number(data.done)}${data.total ? ` of ${number(data.total)}` : ''}` : '…'}</span>}
      </div>
      {start.isError && <p role="alert" className="text-sm text-red-400">{errorText(start.error)}</p>}
      {data?.status === 'failed' && <p role="alert" className="text-sm text-red-400">{data.error}</p>}
      {data?.status === 'completed' && result && <p className="text-xs text-green-300">
        {result.model ? `Cleared ${result.model}: ${number(result.images)} images` : `${number(result.files)} files, ${number(result.lines)} images read · ${number(result.images_updated)} updated · ${number(result.unchanged)} unchanged`}
        {result.unknown_images ? ` · ${number(result.unknown_images)} not in the library` : ''}{result.bad_lines ? ` · ${number(result.bad_lines)} unreadable lines` : ''}
        {' '}· {number(result.sidecars_rewritten)} .txt rewritten{result.warnings?.length ? ` · ${result.warnings.length} warnings: ${result.warnings[0]}` : ''}</p>}
      {models.length > 0 && <table className="text-xs"><tbody>{models.map(([model, entry]) => <tr key={model}>
        <td className="pr-4 text-slate-300">{model}</td>
        <td className="pr-4 tabular-nums text-slate-400">{number(entry.images)} images · {number(entry.tags)} tags</td>
        <td><button className="underline text-red-300 disabled:opacity-40" disabled={running}
          onClick={() => { if (window.confirm(`Remove every ${model} tag and rewrite those .txt files? Its result file can be imported again later.`)) start.mutate({ action: 'clear', model }); }}>Remove</button></td>
      </tr>)}</tbody></table>}
    </div>
  );
}

// Rule-based caption quality control: the prompt's mechanical rules checked in code, flagged captions set aside to redo.
function CaptionCheck() {
  const queryClient = useQueryClient();
  const [minWords, setMinWords] = useState(200);
  const [maxWords, setMaxWords] = useState(350);
  const [problem, setProblem] = useState('');
  const [chosen, setChosen] = useState(null);
  const [viewing, setViewing] = useState(null);
  const [viewerItems, setViewerItems] = useState([]);   // the list as it was when the viewer opened, so it does not shift
  const [showReviewed, setShowReviewed] = useState(false);
  const status = useQuery({ queryKey: ['caption-check'], queryFn: async () => (await api.planner.captionCheck()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false) });
  const result = status.data?.result;
  const items = useQuery({ queryKey: ['caption-check-items', result?.checked_at, problem, showReviewed],
    enabled: Boolean(result?.flagged || (showReviewed && result?.reviewed)),
    queryFn: async () => (await api.planner.captionCheckItems(problem, null, showReviewed)).data.items });
  const start = useMutation({ mutationFn: () => api.planner.startCaptionCheck({ min_words: Number(minWords), max_words: Number(maxWords) }),
    onSuccess: () => status.refetch() });
  const afterRemoval = () => { status.refetch(); queryClient.invalidateQueries({ queryKey: ['caption-check-items'] }); };
  const setAside = useMutation({ mutationFn: (problems) => api.planner.setAsideCaptions(problems), onSuccess: afterRemoval });
  const remove = useMutation({ mutationFn: (problems) => api.planner.deleteFlaggedCaptions(problems), onSuccess: afterRemoval });
  const running = status.data?.status === 'running';
  const counts = Object.entries(result?.counts || {});
  const selected = chosen ?? counts.map(([code]) => code);
  const toggle = (code) => setChosen(selected.includes(code) ? selected.filter((c) => c !== code) : [...selected, code]);
  return (
    <div className="space-y-3 border-t border-slate-800 pt-3">
      <h3 className="text-sm font-semibold text-slate-200">Check captions</h3>
      <p className="text-xs text-slate-400 max-w-3xl">
        Checks every caption in the planner collections against the prompt's rules, in code (no model, so nothing is refused): starts with the
        artist trigger exactly once, word range, one paragraph, no "or", no asterisks or brackets, no "This image shows", every tagged character
        named, no raw tag spelling, quoted text when the tags say the image has text, no refusals or prompt echoes. <b>Set aside</b> renames flagged
        captions to <code>…_nl.txt.flagged</code> (kept to compare); <b>Delete</b> removes them for good (refusals and error messages are
        worth nothing). Either way your captioning script writes them again on its next run.
      </p>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="text-xs text-slate-400">Words at least<input type="number" min="0" className="block w-24 rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm" value={minWords} onChange={(e) => setMinWords(e.target.value)} /></label>
        <label className="text-xs text-slate-400">at most<input type="number" min="1" className="block w-24 rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm" value={maxWords} onChange={(e) => setMaxWords(e.target.value)} /></label>
        <button className={quiet} disabled={running || start.isPending} onClick={() => { setChosen(null); setAside.reset(); remove.reset(); start.mutate(); }}>{running ? 'Checking…' : 'Check captions'}</button>
        {running && <span className="text-xs text-blue-200">{status.data.done ? `${number(status.data.done)} of ${number(status.data.total)} images` : 'Starting…'}</span>}
      </div>
      {start.isError && <p role="alert" className="text-sm text-red-400">{errorText(start.error)}</p>}
      {status.data?.status === 'failed' && <p role="alert" className="text-sm text-red-400">{status.data.error}</p>}
      {result && <div className="space-y-2 text-xs">
        <p className="text-slate-300">{number(result.captioned)} of {number(result.images)} images captioned · {number(result.flagged)} flagged to review
          {result.reviewed ? ` · ${number(result.reviewed)} reviewed and kept` : ''}{result.gone ? ` · ${number(result.gone)} deleted since` : ''}
          · checked {new Date(result.checked_at).toLocaleString()}{!result.facts && ' · build character facts to check character names'}</p>
        {result.reviewed > 0 && <label className="flex items-center gap-2 text-slate-400"><input type="checkbox" checked={showReviewed} onChange={(event) => setShowReviewed(event.target.checked)} />
          Show the {number(result.reviewed)} reviewed captions in the list too</label>}
        {counts.length > 0 && <table><tbody>{counts.map(([code, n]) => <tr key={code}>
          <td className="pr-3"><input type="checkbox" aria-label={`Set aside ${code}`} checked={selected.includes(code)} onChange={() => toggle(code)} /></td>
          <td className="pr-4"><button className={`underline ${problem === code ? 'text-blue-200' : 'text-slate-300'}`} onClick={() => setProblem(problem === code ? '' : code)}>{result.labels?.[code] || code}</button></td>
          <td className="tabular-nums">{number(n)}</td></tr>)}</tbody></table>}
        {counts.length > 0 && <div className="flex flex-wrap items-center gap-3">
          <button className={quiet} disabled={setAside.isPending || remove.isPending || selected.length === 0}
            onClick={() => { if (window.confirm('Rename the flagged captions with the ticked problems to …_nl.txt.flagged, so the captioning script redoes them?')) setAside.mutate(selected); }}>
            Set aside flagged captions</button>
          <button className="rounded border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-100 disabled:opacity-40"
            disabled={setAside.isPending || remove.isPending || selected.length === 0}
            onClick={() => { if (window.confirm(`Delete the flagged caption files with the ticked problems (${number(selected.reduce((sum, code) => sum + (result.counts?.[code] || 0), 0))} flags)? This cannot be undone; the captioning script writes them again on its next run.`)) remove.mutate(selected); }}>
            Delete flagged captions</button>
          {setAside.data && <span className="text-green-300">{number(setAside.data.data.set_aside)} captions set aside; run the captioning script again, then check again.</span>}
          {remove.data && <span className="text-green-300">{number(remove.data.data.deleted)} caption files deleted; run the captioning script again, then check again.</span>}
          {(setAside.isError || remove.isError) && <span className="text-red-400">{errorText(setAside.error || remove.error)}</span>}
        </div>}
        {items.data?.length > 0 && <p className="text-slate-500">Click a row to open the image with its tags, what exactly was found, and the caption editor. <b>Save &amp; next</b> marks a caption reviewed: it leaves this list and later checks until the caption file changes.</p>}
        {items.data?.length > 0 && <div className="max-h-96 overflow-auto rounded border border-slate-800">
          <table className="w-full"><thead><tr className="text-left text-slate-400"><th className="px-2">Folder</th><th className="px-2">Problems</th><th className="px-2">Words</th><th className="px-2">Starts with</th></tr></thead>
            <tbody>{items.data.map((item, index) => <tr key={item.image_id} onClick={() => { setViewerItems(items.data); setViewing(index); }}
              className="cursor-pointer align-top border-t border-slate-800 hover:bg-slate-800/40" title="Open this image">
              <td className="px-2"><Link className="underline text-blue-300" to={`/folder/${item.folder_id}`} onClick={(event) => event.stopPropagation()}>{item.folder}</Link>
                {item.reviewed && <div className="text-emerald-400">✓ reviewed</div>}</td>
              <td className="px-2">{item.problems.map((code) => <div key={code}><span className="text-amber-300">{result.labels?.[code] || code}</span>
                {item.details?.[code] && <span className="text-slate-400"> — {item.details[code]}</span>}</div>)}</td>
              <td className="px-2 tabular-nums">{item.words}</td>
              <td className="px-2 text-slate-400">{item.start}</td></tr>)}</tbody></table></div>}
        {viewing != null && viewerItems[viewing] && <FlaggedCaptionViewer items={viewerItems} index={viewing} setIndex={setViewing}
          onClose={() => { setViewing(null); afterRemoval(); }} labels={result.labels} />}
      </div>}
    </div>
  );
}
