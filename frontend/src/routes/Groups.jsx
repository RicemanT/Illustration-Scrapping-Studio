import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';
import { refreshFolderViews } from '../api/folderCache';
import SyncAllModal from '../components/SyncAllModal';

const field = 'rounded border border-slate-700 bg-[#090d12] px-3 py-2 text-sm';
const button = 'rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-40';
const errorText = (error) => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : String(error?.message || detail || 'Request failed');
};

export default function Groups() {
  const { groupId } = useParams();
  return <GroupPage key={groupId || 'new'} />;
}

function GroupPage() {
  const { groupId } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [name, setName] = useState('');
  const [provider, setProvider] = useState('danbooru');
  const [kind, setKind] = useState('artist');
  const [additionalSources, setAdditionalSources] = useState([]);
  const [text, setText] = useState('');
  const [preview, setPreview] = useState(null);
  const [notice, setNotice] = useState('');
  const [fileError, setFileError] = useState('');
  const [moveId, setMoveId] = useState('');
  const [showSync, setShowSync] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [blockedText, setBlockedText] = useState('');
  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data });
  const folders = useQuery({ queryKey: ['collections'], queryFn: async () => (await api.folders.list()).data });
  const providers = useQuery({ queryKey: ['providers'], queryFn: async () => (await api.providers.list()).data });
  const group = groups.data?.find((item) => item.id === Number(groupId));
  const blocked = useQuery({ queryKey: ['group-blocked', groupId], enabled: Boolean(groupId), queryFn: async () => (await api.groups.blocked(groupId)).data });
  const block = useMutation({ mutationFn: (data) => api.groups.block(group.id, data), onSuccess: async () => { setBlockedText(''); await client.invalidateQueries({ queryKey: ['group-blocked', groupId] }); } });
  const unblock = useMutation({ mutationFn: (folderId) => api.groups.unblock(group.id, folderId), onSuccess: () => client.invalidateQueries({ queryKey: ['group-blocked', groupId] }) });
  const deleteFolder = useMutation({ mutationFn: (folderId) => api.folders.delete(folderId), onSuccess: async () => { setNotice('Collection deleted.'); await refresh(); } });
  const deleteGroup = useMutation({ mutationFn: () => api.groups.delete(group.id), onSuccess: async () => { await refresh(); navigate('/groups'); } });
  const members = folders.data?.filter((folder) => folder.group_id === group?.id) || [];
  const refresh = async () => {
    await refreshFolderViews(client);
    await client.invalidateQueries({ queryKey: ['groups'] });
    await client.invalidateQueries({ queryKey: ['group-blocked'] });
  };
  const create = useMutation({ mutationFn: () => api.groups.create({ name, provider }), onSuccess: async ({ data }) => {
    await refresh(); setName(''); navigate(`/groups/${data.id}`);
  } });
  const inspect = useMutation({ mutationFn: () => api.groups.preview(group.id, text, kind, additionalSources), onSuccess: ({ data }) => setPreview(data) });
  const importList = useMutation({ mutationFn: () => api.groups.import(group.id, text, kind, additionalSources), onSuccess: async ({ data }) => {
    setPreview(data); setNotice(`Created ${data.counts.created} collections. Existing collections and duplicate lines were skipped.`); await refresh();
  } });
  const move = useMutation({ mutationFn: ({ id, destination }) => api.groups.move(id, destination), onSuccess: async () => {
    setMoveId(''); setNotice('Folder and its image/thumbnail directories moved. Existing deletion recovery remains available.');
    await client.invalidateQueries({ queryKey: ['image'] }); await refresh();
  } });
  const replaceProvider = useMutation({ mutationFn: value => api.groups.setProvider(group.id, value), onSuccess: refresh });
  const enableSource = useMutation({ mutationFn: (folderId) => api.groups.enableSource(group.id, folderId), onSuccess: refresh });
  const scrape = useMutation({ mutationFn: (options) => api.groups.sync(group.id, options), onSuccess: ({ data }) => {
    setJobId(data.job_id); setShowSync(false); client.invalidateQueries({ queryKey: ['sync-history-brief'] });
  } });
  const history = useQuery({ queryKey: ['sync-history-brief'], queryFn: async () => (await api.sync.history(50)).data.items, refetchInterval: 5000 });
  const groupHistory = useQuery({ queryKey: ['group-sync-history', groupId], enabled: Boolean(groupId),
    queryFn: async () => (await api.groups.history(groupId)).data.items, refetchInterval: 5000 });
  const currentJobId = jobId || groupHistory.data?.[0]?.job_id;
  const job = useQuery({ queryKey: ['sync-job', currentJobId], enabled: Boolean(currentJobId), queryFn: async () => (await api.sync.job(currentJobId)).data,
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status) ? false : 700 });
  const terminal = ['completed', 'failed', 'canceled'].includes(job.data?.status);
  useEffect(() => { if (terminal) { refreshFolderViews(client); client.invalidateQueries({ queryKey: ['sync-history-brief'] }); } }, [terminal, job.data?.job_id, client]);
  const cancel = useMutation({ mutationFn: () => api.sync.cancel(currentJobId), onSuccess: () => client.invalidateQueries({ queryKey: ['sync-job', currentJobId] }) });
  useEffect(() => {
    setText(''); setPreview(null); setNotice(''); setFileError(''); setMoveId(''); setJobId(null); setShowSync(false);
    inspect.reset(); importList.reset(); move.reset(); scrape.reset();
  }, [groupId]);
  const busy = inspect.isPending || importList.isPending;
  const activeSync = history.data?.some((job) => ['queued', 'running', 'cancelling'].includes(job.status));

  return <div className="space-y-5 max-w-6xl">
    <h1 className="text-xl font-semibold">Collection groups</h1>
    <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
      <input aria-label="Group name" className={field} placeholder="New group name" value={name} maxLength={100} required onChange={(event) => setName(event.target.value)} />
      <select aria-label="Group provider" title="Batch provider for Scrape group; collections can have additional sources" className={field} value={provider} onChange={(event) => setProvider(event.target.value)}>
        {(providers.data?.items || [{ name: 'danbooru' }, { name: 'e621' }]).map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
      </select>
      <button className={button} disabled={create.isPending}>Create group</button>
    </form>
    <div className="flex flex-wrap gap-2">{groups.data?.map((item) => <Link className={`${field} ${item.id === group?.id ? 'text-blue-300 border-blue-500' : ''}`} key={item.id} to={`/groups/${item.id}`}>{item.name} / {item.provider}</Link>)}</div>
    {[groups.error, folders.error, create.error, inspect.error, importList.error, move.error, scrape.error, cancel.error, enableSource.error, replaceProvider.error, deleteGroup.error, deleteFolder.error, blocked.error, block.error, unblock.error].filter(Boolean).map((error, index) => <p role="alert" className="text-red-400" key={index}>{errorText(error)}</p>)}
    {notice && <p role="status" className="text-green-300">{notice}</p>}
    {!group && <p className="text-slate-400">Create or select a group to import a collection list and scrape its members separately.</p>}
    {group && <>
      <section className="rounded border border-slate-800 p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3"><h2 className="flex-1 font-semibold">{group.name} · {members.length} collections · {members.reduce((sum, folder) => sum + folder.image_count, 0)} images</h2>
          <button className={button} disabled={group.provider === 'retired' || activeSync || !members.length || scrape.isPending} onClick={() => { scrape.reset(); setShowSync(true); }}>Scrape group</button>
          <button className="rounded border border-red-800 px-3 py-2 text-sm text-red-200" disabled={deleteGroup.isPending || activeSync || deleteFolder.isPending} onClick={() => { if (window.confirm(`Delete group "${group.name}" and all ${members.length} collections and images? This cannot be undone.`)) deleteGroup.mutate(); }}>Delete group</button></div>
        <label className="block text-sm">Group scrape provider <select aria-label="Group scrape provider" className={field} value={group.provider} disabled={activeSync || replaceProvider.isPending} onChange={e => replaceProvider.mutate(e.target.value)}>
          {group.provider === 'retired' && <option value="retired" disabled>Choose a replacement provider</option>}
          {providers.data?.items?.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
        </select><span className="ml-2 text-xs text-slate-400">Changes batch routing only; enable the source and set its query in each collection.</span></label>
        <p className="text-xs text-slate-400">Provider: {group.provider}. Storage: images/{group.slug}/&lt;collection&gt;/ and thumbnails/{group.slug}/&lt;collection&gt;/</p>
        <p className="text-xs text-slate-400">Group scraping uses only {group.provider} for enabled members that have that source enabled. Other providers and groups are excluded.</p>
        {job.data && <div className="text-sm space-y-1"><p>{job.data.status}: {job.data.progress?.message}</p><p>{job.data.progress?.completed || 0}/{job.data.progress?.total || 0} collections · {job.data.progress?.new_images || 0} new images · {job.data.progress?.errors || 0} errors</p>
          {!terminal && <button className={button} disabled={cancel.isPending} onClick={() => cancel.mutate()}>Stop group scrape</button>}
          {job.data.error && <p className="text-red-400">{job.data.error}</p>}
          {job.data.result?.sources?.filter((source) => source.error).map((source) => <p className="text-red-400" key={source.folder_id}>{source.folder_name}: {source.error}</p>)}
        </div>}
      </section>
      <section className="rounded border border-slate-800 p-4 space-y-3">
        <h2 className="font-semibold">Protected folders</h2>
        <p className="text-xs text-slate-400">Protected folders are skipped by Scrape group, Sync All, and scheduled syncs. Explicit single-collection scraping remains available. Add every current member, paste names, or upload a UTF-8 text file. Finish running jobs before changing protection.</p>
        <div className="flex flex-wrap gap-2"><button className={button} disabled={block.isPending || unblock.isPending || activeSync || !members.length} onClick={() => block.mutate({ all: true })}>Protect all current folders</button><button className={button} disabled={block.isPending || activeSync || !blockedText.trim()} onClick={() => block.mutate({ text: blockedText })}>Add pasted names</button></div>
        <input type="file" aria-label="Protected folders text file" accept=".txt,text/plain" disabled={block.isPending || activeSync} onChange={async event => {
          const file = event.target.files?.[0]; if (!file) return;
          setFileError('');
          try {
            if (file.size > 1024 * 1024) throw new Error('Use a file no larger than 1 MiB');
            setBlockedText(new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()));
          } catch (error) { setFileError(error.message); setBlockedText(''); }
        }} />
        <p className="text-xs text-slate-400">{blocked.data?.length || 0} protected collections. Unknown names are reported without applying partial changes.</p>
        <textarea aria-label="Protected folder names" className={`${field} block w-full h-20`} value={blockedText} onChange={e => setBlockedText(e.target.value)} placeholder="folder_name" />
        <div className="max-h-32 overflow-auto text-sm divide-y divide-slate-800">{blocked.data?.map(item => <div className="flex justify-between py-1" key={item.id}><span>{item.name}</span><button className="text-xs text-red-300" disabled={unblock.isPending || block.isPending || activeSync} onClick={() => unblock.mutate(item.id)}>Unprotect</button></div>)}</div>
      </section>
      <section className="rounded border border-slate-800 p-4 space-y-3">
        <h2 className="font-semibold">Import collection list</h2>
        <label className="block text-sm">List type <select aria-label="List type" className={field} value={kind} disabled={busy} onChange={e => { setKind(e.target.value); setAdditionalSources([]); setPreview(null); setNotice(''); }}><option value="artist">Artists</option><option value="character">Characters</option><option value="tag">Tags / queries</option></select></label>
        <fieldset className="space-y-2"><legend className="text-sm">Sources for new collections (select multiple)</legend>
          <p className="text-xs text-slate-400">{group.provider} is included for group scraping. Extra sources can be synced from each collection or Sync All. Existing collections are skipped, not modified by this import.</p>
          <div className="flex flex-wrap gap-3">{providers.data?.items?.map(item => <label key={item.name} className="text-sm flex items-center gap-2">
            <input type="checkbox" aria-label={`Bulk source ${item.name}`} checked={item.name === group.provider || additionalSources.includes(item.name)} disabled={busy || item.name === group.provider || !(item.collection_types || ['artist','character','tag']).includes(kind)} onChange={event => { setAdditionalSources(old => event.target.checked ? [...old,item.name] : old.filter(name => name !== item.name)); setPreview(null); }} />
            {item.name}{!item.available ? ' (setup needed)' : ''}
          </label>)}</div>
        </fieldset>
        <p className="text-sm text-slate-400">Upload a UTF-8 .txt file or paste one artist, character, or tag query per line. Choose the list type below; tag queries keep their spaces and underscores. Preview, then create the folders; scraping starts separately. Maximum 5,000 lines / 1 MiB.</p>
        <input key={groupId} aria-label="Collection text file" type="file" accept=".txt,text/plain" disabled={busy} onChange={async (event) => {
          const file = event.target.files?.[0]; if (!file) return;
          setPreview(null); setFileError(''); setNotice('');
          try { if (file.size > 1024 * 1024) throw new Error('Use a file no larger than 1 MiB');
            setText(new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()));
          } catch (error) { setFileError(error.message); setText(''); }
        }} />
        {fileError && <p role="alert" className="text-red-400">{fileError}</p>}
        <textarea aria-label="Collection list" className={`${field} block w-full h-40`} value={text} disabled={busy} onChange={(event) => { setText(event.target.value); setPreview(null); setNotice(''); }} placeholder={'artist_one\nartist_two'} />
        <button className={button} disabled={busy || group.provider === 'retired' || !text.trim()} onClick={() => { setNotice(''); inspect.mutate(); }}>Preview list</button>
        {preview && <div className="space-y-2"><p>{preview.counts.new} new · {preview.counts.existing} existing · {preview.counts.duplicate} duplicate lines · {preview.counts.invalid} invalid · {preview.counts.created} created</p>
          <div className="max-h-52 overflow-auto text-sm">{preview.items.map((item) => <div key={item.line}>{item.line}. {item.name || item.artist} ({item.type || kind}) — {item.status} {item.reason || ""}</div>)}</div>
          <button className={button} disabled={busy || !preview.counts.new || preview.counts.invalid > 0} onClick={() => importList.mutate()}>Create collections</button>
        </div>}
      </section>
      <section className="rounded border border-slate-800 p-4 space-y-3">
        <h2 className="font-semibold">Collections in this group</h2>
        <div className="flex flex-wrap gap-2"><select aria-label="Existing folder to move" className={field} value={moveId} onChange={(event) => setMoveId(event.target.value)}>
          <option value="">Choose an existing folder to move here</option>{folders.data?.filter((folder) => folder.group_id !== group.id).map((folder) => <option key={folder.id} value={folder.id}>{folder.name} ({groups.data?.find((item) => item.id === folder.group_id)?.name || 'Ungrouped'})</option>)}
        </select><button className={button} disabled={!moveId || move.isPending} onClick={() => move.mutate({ id: moveId, destination: group.id })}>Move into group</button></div>
        <p className="text-xs text-slate-400">Moving also moves the image and thumbnail directories. Finish or cancel pending jobs first. A move preserves the artist’s configured sources; use Enable {group.provider} below if needed. Earlier exports keep their original paths.</p>
        <div className="max-h-96 overflow-auto divide-y divide-slate-800">{members.map((folder) => <div key={folder.id} className="flex justify-between items-center py-2 gap-3">
          <Link to={`/folder/${folder.id}`} className="text-blue-300">{folder.name} · {folder.image_count} images</Link>
          {folder.sources.some((source) => source.provider === group.provider && source.enabled)
            ? <span className="text-xs text-slate-400">{blocked.data?.some(item => item.id === folder.id) ? 'Protected from bulk scraping' : folder.enabled ? 'Ready' : 'Folder disabled'}</span>
            : <button className="text-xs text-blue-300" disabled={enableSource.isPending || group.provider === 'retired'} onClick={() => enableSource.mutate(folder.id)}>Enable {group.provider}</button>}
          <button className="text-xs text-slate-400" disabled={move.isPending} onClick={() => move.mutate({ id: folder.id, destination: null })}>Move to Ungrouped</button>
          <button className="text-xs text-red-300" disabled={move.isPending || deleteFolder.isPending} onClick={() => { if (window.confirm(`Delete collection "${folder.name}" and all of its images? This cannot be undone.`)) deleteFolder.mutate(folder.id); }}>Delete</button>
        </div>)}</div>
      </section>
    </>}
    {showSync && group && <SyncAllModal group={group} onClose={() => setShowSync(false)} onStart={(options) => scrape.mutate(options)} pending={scrape.isPending} error={scrape.error} />}
  </div>;
}
