import React, { useState } from 'react';

function PathRow({ label, value, prospective = false }) {
  const [message, setMessage] = useState('');
  const path = typeof value === 'string' ? value : value?.path;
  const unsafe = value?.within_expected_root === false;
  const missing = !prospective && value?.exists === false;
  async function copy() {
    try {
      await navigator.clipboard.writeText(path);
      setMessage('Copied');
    } catch {
      setMessage('Select the path below and copy manually.');
    }
  }
  return <div className="space-y-1">
    <div className="flex items-center justify-between gap-2 text-xs">
      <span className="font-medium text-slate-300">{label}</span>
      {path && <button type="button" onClick={copy} className="text-blue-300" aria-label={`Copy ${label} path`}>Copy</button>}
    </div>
    <code className="block select-all break-all rounded bg-slate-950 p-2 text-xs text-slate-200">{path || 'Not recorded'}</code>
    {unsafe && <p className="text-xs text-red-400">Warning: outside the expected storage directory. Not inspected.</p>}
    {missing && <p className="text-xs text-amber-300">Missing on the backend filesystem.</p>}
    {message && <p role="status" className="text-xs text-slate-400">{message}</p>}
  </div>;
}

export default function ImageStorageLocations({ locations }) {
  if (!locations) return <p className="text-xs text-slate-400">File locations unavailable until image details load.</p>;
  const { files, trash, database, metadata_records: records } = locations;
  return <section className="space-y-3 rounded border border-slate-700 bg-slate-900 p-3">
    <h3 className="text-sm font-semibold text-slate-100">File locations</h3>
    <p className="text-xs text-slate-400">Absolute paths on the active backend, not necessarily this browser's computer. The stored image is the normalized training image, not a retained raw download.</p>
    <PathRow label="Library root" value={locations.library_root} />
    <PathRow label="Full-quality training image" value={files.image} />
    <PathRow label="UI thumbnail" value={files.thumbnail} />
    <PathRow label="Ground-truth sidecar (.txt)" value={files.sidecar} />
    <PathRow label="Metadata database" value={database} />
    <p className="text-xs text-slate-400">Source metadata and review/tag edits are database records, not separate JSON files. Image record: {records.image_id}; source records: {records.source_ids.join(', ') || 'none'}.</p>
    <PathRow label={trash.is_template ? 'Trash destination pattern' : 'Recovery snapshot directory'} value={trash.root} />
    <details className="space-y-3">
      <summary className="cursor-pointer text-sm text-slate-200">{trash.is_template ? 'Trash destination on batch deletion' : 'Actual recovery snapshot'}</summary>
      <p className="text-xs text-slate-400">{trash.is_template ? 'These are destination patterns, not existing files. {deletion-token} is generated when you delete.' : 'These paths come from the saved recovery manifest.'} Only the latest deletion batch per folder is recoverable; the next batch replaces it. This is app trash, not the Windows Recycle Bin.</p>
      {Object.entries(trash.files).map(([key, value]) => <PathRow key={key} label={`Trash ${key}`} value={value} prospective={trash.is_template} />)}
    </details>
    <details className="space-y-3">
      <summary className="cursor-pointer text-sm text-slate-200">Temporary downloads and processing</summary>
      <PathRow label="Scratch directory" value={locations.scratch_root} />
      <p className="text-xs text-slate-400">Raw videos, GIFs, archives and intermediate frames are temporary and normally removed after processing. They are not retained originals.</p>
    </details>
  </section>;
}
