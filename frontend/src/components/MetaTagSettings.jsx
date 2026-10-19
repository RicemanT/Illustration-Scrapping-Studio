import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

// The metadata tags worth training on (medium, software, artifacts): kept in the ground truth while the meta
// section stays off.
export default function MetaTagSettings() {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ['meta-tags'],
    queryFn: async () => (await api.settings.metaTags()).data,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 1500 : false),
  });
  const [text, setText] = useState('');
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (status.data && !loaded) { setText((status.data.allowlist || []).join('\n')); setLoaded(true); }
  }, [status.data, loaded]);
  const running = status.data?.status === 'running';
  useEffect(() => {
    if (status.data?.status === 'done') {
      queryClient.invalidateQueries({ queryKey: ['image'] });
      queryClient.invalidateQueries({ queryKey: ['collection-images'] });
    }
  }, [status.data?.status, status.data?.finished_at]);
  const save = useMutation({
    mutationFn: () => api.settings.saveMetaTags(text),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['meta-tags'] }),
  });
  const download = useMutation({
    mutationFn: async () => (await api.settings.metaTagsExport()).data,
    onSuccess: (content) => {
      const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = 'meta-tags.txt';
      link.click();
      URL.revokeObjectURL(url);
    },
  });
  const loadFile = (event) => {
    const file = event.target.files?.[0];
    if (file) file.text().then(setText);
    event.target.value = '';
  };
  const count = text.split('\n').map((line) => line.split('#')[0].trim()).filter(Boolean).length;
  const saved = status.data?.allowlist?.length ?? 0;
  return (
    <section className="bg-[#0c1219] border border-[#202a34] rounded p-4">
      <h2 className="font-semibold text-slate-100">Metadata tags to keep</h2>
      <p className="mt-1 text-xs text-slate-400">
        Most booru metadata tags are noise for training (commentary, translation request), so the metadata section is off. A few
        describe the image: the medium (traditional media, watercolor), the software (procreate, clip studio paint), artifacts
        (jpeg artifacts). Tags on this list join every image's ground truth when the image has them, after its general tags.
      </p>
      <ol className="mt-2 list-decimal pl-5 text-xs text-slate-400">
        <li><button type="button" onClick={() => download.mutate()} disabled={download.isPending} className="text-sky-300 underline disabled:opacity-50">
          {download.isPending ? 'Preparing…' : 'Download the library\'s metadata tags'}</button> (meta-tags.txt, most common first, with image counts;
          names with “commentary” or “request” come commented out).</li>
        <li>Delete the lines you do not want, or comment them out with #.</li>
        <li>Load the file or paste it below, then Save. Every sidecar is rewritten.</li>
      </ol>
      <textarea aria-label="Metadata tags to keep" value={text} onChange={(event) => setText(event.target.value)} rows={8} spellCheck={false}
        placeholder={'traditional media\nprocreate (medium)\njpeg artifacts'}
        className="mt-3 w-full rounded border border-[#202a34] bg-[#090d12] p-2 font-mono text-xs text-slate-200" />
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <label className="cursor-pointer rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300">Load file
          <input type="file" accept=".txt,text/plain" onChange={loadFile} className="hidden" />
        </label>
        <button type="button" onClick={() => save.mutate()} disabled={running || save.isPending}
          className="rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-50">{running ? 'Rewriting sidecars…' : `Save (${count} tags)`}</button>
        <span className="text-xs text-slate-500">{saved} saved</span>
        {running && <span className="text-xs text-sky-300">{status.data.done.toLocaleString()} / {(status.data.total || 0).toLocaleString()} sidecars</span>}
        {status.data?.status === 'done' && status.data.result && <span className="text-xs text-emerald-300">
          {status.data.result.tags} tags kept · {status.data.result.sidecars.toLocaleString()} sidecars rewritten
          {status.data.result.warnings ? ` · ${status.data.result.warnings} warnings` : ''}</span>}
      </div>
      {status.data?.status === 'error' && <p className="mt-2 text-xs text-red-400">{status.data.error}</p>}
      {(save.isError || download.isError) && <p className="mt-2 text-xs text-red-400">{(save.error || download.error).response?.data?.detail || (save.error || download.error).message}</p>}
    </section>
  );
}
