import { refreshFolderViews } from '../api/folderCache';
import React, { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';

const formatBytes = (bytes) => {
  if (!Number.isFinite(bytes)) return 'Unknown size';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

function ImageChoice({ image, label }) {
  const preview = image.thumb_path
    ? backendAssetUrl(`/static/thumbnails/${image.thumb_path}`)
    : backendAssetUrl(`/static/images/${image.path}`);
  return (
    <div className="min-w-0 rounded border border-[#202a34] bg-[#090d12] overflow-hidden">
      <div className="px-2 py-1 text-xs font-medium text-slate-300 border-b border-[#202a34]">{label}</div>
      <img
        src={preview}
        alt={`Candidate ${image.id}`}
        className="w-full h-56 object-contain bg-black"
        loading="lazy"
      />
      <div className="p-2 text-xs text-slate-400 space-y-1">
        <div>{image.width} × {image.height} · {image.format.toUpperCase()} · {formatBytes(image.file_size)}</div>
        <div className="truncate text-slate-400" title={image.sha256}>Image {image.id} · {image.sha256.slice(0, 12)}</div>
      </div>
    </div>
  );
}

function DuplicateReview({ collectionId }) {
  const queryClient = useQueryClient();
  const [profile, setProfile] = useState('balanced');
  const [jobId, setJobId] = useState(null);
  const [error, setError] = useState(null);

  const candidatesQuery = useQuery({
    queryKey: ['duplicate-candidates', collectionId],
    queryFn: async () => (await api.duplicates.list(collectionId)).data,
  });

  const scanJobQuery = useQuery({
    queryKey: ['duplicate-scan-job', jobId],
    queryFn: async () => (await api.duplicates.job(jobId)).data,
    enabled: Boolean(jobId),
    refetchInterval: (query) => ['completed', 'failed'].includes(query.state.data?.status) ? false : 600,
  });

  const scanMutation = useMutation({
    mutationFn: async () => (await api.duplicates.scan(collectionId, profile)).data,
    onSuccess: (job) => { setError(null); setJobId(job.job_id); },
    onError: (requestError) => setError(requestError.response?.data?.detail || requestError.message),
  });

  const resolveMutation = useMutation({
    mutationFn: ({ candidateId, action }) => api.duplicates.resolve(candidateId, action),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ['duplicate-candidates', collectionId] });
      refreshFolderViews(queryClient, collectionId);
    },
    onError: (requestError) => setError(requestError.response?.data?.detail || requestError.message),
  });

  const scanJob = scanJobQuery.data;
  useEffect(() => {
    if (scanJob?.status === 'completed') {
      queryClient.invalidateQueries({ queryKey: ['duplicate-candidates', collectionId] });
    }
    if (scanJob?.status === 'failed') setError(scanJob.error);
  }, [scanJob?.status, scanJob?.error, collectionId, queryClient]);

  const resolve = (candidate, action) => {
    if (['keep_a', 'keep_b', 'keep_highest_quality'].includes(action)) {
      const message = action === 'keep_highest_quality'
        ? 'Merge this pair and remove the lower-quality file? Source records and tags will be moved to the better image in this folder.'
        : `Merge this pair and permanently remove ${action === 'keep_a' ? 'image B' : 'image A'}? Source records and tags will be preserved on the kept image.`;
      if (!window.confirm(message)) return;
    }
    resolveMutation.mutate({ candidateId: candidate.id, action });
  };

  const progress = scanJob?.progress;
  const progressPercent = progress?.total ? Math.round((progress.completed / progress.total) * 100) : 0;
  const candidates = candidatesQuery.data?.items || [];

  return (
    <div className="p-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 rounded border border-[#202a34] bg-[#0a0f15] p-3">
        <div className="mr-auto">
          <div className="text-sm font-medium text-slate-200">Visual duplicate review</div>
          <div className="mt-1 text-xs text-slate-400">Exact SHA and MD5 matches are reused automatically. Visual matches always wait for your decision.</div>
        </div>
        <select value={profile} onChange={(event) => setProfile(event.target.value)} className="px-2 py-1 bg-[#090d12] border border-[#202a34] rounded text-xs">
          <option value="strict">Strict · fewer false matches</option>
          <option value="balanced">Balanced · recommended</option>
          <option value="broad">Broad · catches heavier edits</option>
        </select>
        <button type="button" onClick={() => scanMutation.mutate()} disabled={scanMutation.isPending || ['queued', 'running'].includes(scanJob?.status)} className="px-3 py-1 rounded bg-[#344a73] text-white text-xs disabled:opacity-50">
          {['queued', 'running'].includes(scanJob?.status) ? 'Scanning…' : 'Scan folder'}
        </button>
      </div>

      {progress && ['queued', 'running'].includes(scanJob?.status) && (
        <div className="rounded border border-[#202a34] p-3 text-xs text-slate-400">
          <div className="flex justify-between"><span>{progress.message}</span><span>{progressPercent}%</span></div>
          <div className="mt-2 h-1.5 rounded bg-[#0c1219] overflow-hidden"><div className="h-full bg-blue-500 transition-all" style={{ width: `${progressPercent}%` }} /></div>
          {Number.isFinite(progress.candidates) && <div className="mt-1">{progress.candidates} possible pairs found</div>}
        </div>
      )}

      {scanJob?.status === 'completed' && scanJob.result && (
        <div className="rounded border border-emerald-900/60 bg-emerald-950/20 p-3 text-xs text-emerald-300">
          Scanned {scanJob.result.images_scanned} images · added {scanJob.result.fingerprints_added} fingerprints · {scanJob.result.pending_candidates} pairs waiting for review
          {scanJob.result.missing_files > 0 && <span className="text-amber-300"> · {scanJob.result.missing_files} missing files</span>}
        </div>
      )}
      {error && <div className="rounded border border-red-900/60 bg-red-950/20 p-3 text-xs text-red-300">{error}</div>}

      {candidatesQuery.isLoading ? (
        <div className="py-12 text-center text-sm text-slate-400">Loading duplicate candidates…</div>
      ) : candidates.length === 0 ? (
        <div className="py-12 text-center text-sm text-slate-400">No visual duplicate pairs are waiting. Run a scan after importing new images.</div>
      ) : candidates.map((candidate) => (
        <div key={candidate.id} className="rounded border border-[#202a34] bg-[#0c1219] p-3 space-y-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <span className="text-lg font-semibold text-blue-300">{candidate.visual_similarity.toFixed(1)}% similar</span>
            <span className="text-slate-400">pHash distance {candidate.phash_distance}</span>
            <span className="text-slate-400">dHash distance {candidate.dhash_distance}</span>
            <span className="text-slate-400">Gradient distance {candidate.gradient_distance}</span>
            <span className="text-slate-400">Color difference {candidate.color_distance}%</span>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <ImageChoice image={candidate.image_a} label="Image A" />
            <ImageChoice image={candidate.image_b} label="Image B" />
          </div>
          <div className="text-[11px] text-slate-400">Signals: {candidate.methods.join(' · ')}</div>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => resolve(candidate, 'keep_a')} disabled={resolveMutation.isPending} className="px-3 py-1 rounded bg-[#273451] text-blue-200 text-xs disabled:opacity-50">Keep A and merge</button>
            <button type="button" onClick={() => resolve(candidate, 'keep_highest_quality')} disabled={resolveMutation.isPending} className="px-3 py-1 rounded bg-[#344a73] text-white text-xs disabled:opacity-50">Keep highest quality</button>
            <button type="button" onClick={() => resolve(candidate, 'keep_b')} disabled={resolveMutation.isPending} className="px-3 py-1 rounded bg-[#273451] text-blue-200 text-xs disabled:opacity-50">Keep B and merge</button>
            <button type="button" onClick={() => resolve(candidate, 'keep_both')} disabled={resolveMutation.isPending} className="px-3 py-1 rounded border border-[#3a4656] text-slate-300 text-xs disabled:opacity-50">Keep both</button>
            <button type="button" onClick={() => resolve(candidate, 'not_duplicate')} disabled={resolveMutation.isPending} className="px-3 py-1 rounded border border-[#3a4656] text-slate-400 text-xs disabled:opacity-50">Not the same image</button>
          </div>
        </div>
      ))}
    </div>
  );
}

export default DuplicateReview;
