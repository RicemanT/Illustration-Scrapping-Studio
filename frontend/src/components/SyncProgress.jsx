import React from 'react';
import { Link } from 'react-router-dom';

export const formatBytes = (bytes) => {
  if (bytes === null || bytes === undefined) return 'size unknown';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(2)} MB`;
};

export const formatEta = (seconds) => {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return 'ETA unavailable';
  if (seconds < 1) return 'ETA <1s';
  const rounded = Math.ceil(seconds);
  const minutes = Math.floor(rounded / 60);
  const remaining = rounded % 60;
  return minutes ? `ETA ${minutes}m ${remaining}s` : `ETA ${remaining}s`;
};

const formatElapsed = (seconds) => {
  if (!Number.isFinite(seconds)) return '0s elapsed';
  const rounded = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(rounded / 60);
  const remaining = rounded % 60;
  return minutes ? `${minutes}m ${remaining}s elapsed` : `${remaining}s elapsed`;
};

export const formatTransferSummary = (transfer) => {
  if (!transfer?.bytes_downloaded) return null;
  const parts = [formatBytes(transfer.bytes_downloaded)];
  if (transfer.bytes_total) parts[0] += ` / ${formatBytes(transfer.bytes_total)}`;
  if (transfer.speed_bps) parts.push(`${(transfer.speed_bps / 1024 ** 2).toFixed(2)} MB/s`);
  if (transfer.elapsed_seconds) parts.push(`${transfer.elapsed_seconds.toFixed(1)}s`);
  return parts.join(' · ');
};

export function FileTransferProgress({ transfer }) {
  if (!transfer) return null;
  const total = transfer.bytes_total;
  const downloaded = transfer.bytes_downloaded || 0;
  const determinate = Boolean(total);
  const percent = determinate ? Math.min(100, transfer.percent ?? ((downloaded / total) * 100)) : null;
  const stage = String(transfer.stage || 'downloading').replaceAll('_', ' ');
  const speed = transfer.speed_bps ? `${(transfer.speed_bps / 1024 ** 2).toFixed(2)} MB/s` : 'measuring speed';
  const isActiveDownload = stage === 'downloading' || stage === 'starting';

  return (
    <div className="my-2 rounded border border-[#243244] bg-[#090d12] p-2 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-slate-300 capitalize">{stage} {transfer.format || 'file'} for {transfer.remote_id}</span>
        <span className="text-slate-400">
          {formatBytes(downloaded)}{total ? ` / ${formatBytes(total)}` : ''} · {speed} · {isActiveDownload ? formatEta(transfer.eta_seconds) : stage === 'complete' ? 'complete' : 'processing locally'}
        </span>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded bg-[#0c1219]">
        <div
          className={`h-full transition-all ${determinate ? 'bg-cyan-500' : 'w-1/3 animate-pulse bg-cyan-500'}`}
          style={determinate ? { width: `${percent}%` } : undefined}
        />
      </div>
    </div>
  );
}

export function ActiveTransferProgress({ progress }) {
  const activeFiles = progress?.active_files || [];
  if (activeFiles.length === 0) {
    return <FileTransferProgress transfer={progress?.current_file} />;
  }
  return (
    <div className="my-2 space-y-2">
      <div className="text-[11px] text-slate-400">{activeFiles.length} active of {progress?.workers || activeFiles.length} configured workers</div>
      {activeFiles.map((transfer, index) => (
        <FileTransferProgress key={`${transfer.remote_id || 'file'}-${transfer.stage || 'active'}-${index}`} transfer={transfer} />
      ))}
    </div>
  );
}

export function SyncProgressDetails({ job, onCancel, canceling = false, showResult = true }) {
  if (!job) return null;
  const rootProgress = job.progress || {};
  const hasPairProgress = job.kind === 'all' && Boolean(rootProgress.pair_progress);
  const progress = hasPairProgress
    ? rootProgress.pair_progress
    : rootProgress;
  const finished = ['completed', 'failed', 'canceled'].includes(job.status);
  const percent = progress.total ? Math.round(((progress.completed || 0) / progress.total) * 100) : 8;
  const target = job.kind === 'all' && rootProgress.current_folder
    ? `${rootProgress.current_folder} / ${rootProgress.current_provider}`
    : null;
  const message = rootProgress.message || 'Preparing sync...';
  const showTarget = target && !String(message).startsWith(`${target}:`);

  return (
    <>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
        <div>
          <span>{message}</span>
          {showTarget && <span className="ml-2 text-slate-400">({target})</span>}
        </div>
        <div className="flex items-center gap-3">
          {job.kind === 'all' && <span>{rootProgress.completed || 0}/{rootProgress.total || '?'} sources</span>}
          {(job.kind !== 'all' || hasPairProgress) && <span>{progress.completed || 0}/{progress.total || '?'} posts</span>}
          {!finished && onCancel && (
            <button type="button" onClick={onCancel} disabled={canceling || job.status === 'cancelling'} className="rounded bg-red-900/70 px-3 py-1 text-xs font-medium text-red-100 hover:bg-red-800 disabled:opacity-50">
              {canceling || job.status === 'cancelling' ? 'Stopping...' : job.kind === 'all' ? 'Stop Sync All' : 'Stop sync'}
            </button>
          )}
        </div>
      </div>
      <Link className="text-xs text-blue-300 underline" to={`/logs?job_id=${encodeURIComponent(job.job_id)}`}>View job diagnostics</Link>
      <ActiveTransferProgress progress={progress} />
      {progress.phase === 'discover' && (
        <div className="mb-2 text-[11px] text-slate-400">
          ArtStation original matching: {progress.completed || 0}/{progress.total || '?'} projects
          {' | '}{progress.workers || 1} parallel worker{progress.workers === 1 ? '' : 's'}
          {' | '}{formatElapsed(progress.elapsed_seconds)}
          {' | '}{formatEta(progress.eta_seconds)}
        </div>
      )}
      <div className="h-2 overflow-hidden rounded bg-[#0c1219]"><div className="h-full bg-blue-500 transition-all" style={{ width: `${percent}%` }} /></div>
      {job.logs?.length > 0 && (
        <div className="mt-2 max-h-24 overflow-y-auto text-xs text-slate-400">
          {job.logs.slice(-5).map((log, index) => (
            <div key={`${log.at}-${index}`} className="flex flex-wrap justify-between gap-x-3">
              <span>{log.message}</span>
              {formatTransferSummary(log.current_file) && <span className="text-slate-400">{formatTransferSummary(log.current_file)}</span>}
            </div>
          ))}
        </div>
      )}
      {showResult && job.status === 'completed' && job.result && <div className="mt-2 text-emerald-400">Completed: {job.result.new_images} new, {job.result.skipped} skipped, {job.result.errors} errors</div>}
      {showResult && job.status === 'failed' && <div className="mt-2 text-red-400">Sync failed: {job.error}</div>}
      {showResult && job.status === 'canceled' && <div className="mt-2 text-amber-400">Sync canceled. Completed image pairs were kept; no further posts were started.</div>}
    </>
  );
}
