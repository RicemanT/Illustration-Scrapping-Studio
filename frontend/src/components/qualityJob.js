import { useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

const number = (value) => Number(value || 0).toLocaleString();

// The automatic quality job is shared by the Planner page and collection headers.
export function useQualityJob() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['quality-job'],
    queryFn: async () => (await api.planner.qualityJob()).data,
    refetchInterval: (state) => (state.state.data?.status === 'running' ? 1500 : false),
  });
  const running = query.data?.status === 'running';
  const wasRunning = useRef(false);
  useEffect(() => {
    if (running) { wasRunning.current = true; return; }
    if (!wasRunning.current) return;
    wasRunning.current = false;
    for (const key of [['planner-folder'], ['collection-images'], ['image'], ['quality-status'], ['quality-thresholds']]) {
      queryClient.invalidateQueries({ queryKey: key });
    }
  }, [running, queryClient]);
  const start = useMutation({
    mutationFn: (body) => api.planner.applyQuality(body),
    onSuccess: ({ data }) => { queryClient.setQueryData(['quality-job'], data); query.refetch(); },
  });
  return { job: query.data, running, start };
}

export function describeQualityJob(job) {
  if (!job || job.status === 'idle') return '';
  if (job.status === 'running') {
    if (job.phase === 'statistics') return `Counting harvested scores… ${number(job.read)} of about ${number(job.total)} posts`;
    if (job.phase === 'assigning') return `Assigning quality tags… ${number(job.done)} of ${number(job.total)} collections`;
    return 'Starting…';
  }
  if (job.status === 'failed') return `Automatic quality tagging failed: ${job.error}`;
  const r = job.result;
  if (!r) return '';
  const assigned = ['masterpiece', 'best quality', 'low quality'].filter((tag) => r.assigned?.[tag]).map((tag) => `${number(r.assigned[tag])} ${tag}`);
  const aesthetic = ['very aesthetic', 'aesthetic'].filter((tag) => r.aesthetic?.[tag]).map((tag) => `${number(r.aesthetic[tag])} ${tag}`);
  return [
    `Done: ${number(r.images)} images in ${number(r.folders)} ${r.folders === 1 ? 'collection' : 'collections'}`,
    assigned.length ? assigned.join(', ') : 'no quality tags',
    aesthetic.length ? aesthetic.join(', ') : null,
    r.not_analysed ? `${number(r.not_analysed)} not analysed (no aesthetic tag)` : null,
    `${number(r.normal)} normal`,
    r.kept_manual ? `${number(r.kept_manual)} hand-set marks kept` : null,
    r.no_score ? `${number(r.no_score)} without a usable score` : null,
    r.locked_folders ? `${number(r.locked_folders)} accepted ${r.locked_folders === 1 ? 'collection' : 'collections'} skipped (tick “Include accepted collections” to tag them too)` : null,
  ].filter(Boolean).join(' · ');
}

const SITE_LABEL = { danbooru: 'Danbooru', gelbooru: 'Gelbooru', e621: 'e621' };
const METRIC_LABEL = { score: 'score', favorites: 'favorites' };

// One line explaining an image's automatic suggestion.
export function describeAuto(tag, info) {
  if (!info) return null;
  if (info.reason) return `Auto: no suggestion (${info.reason})`;
  const scope = [info.year ? String(info.year) : 'all years', info.rating || 'all ratings'].join(' ');
  return `Auto: ${tag || 'normal'} · ${SITE_LABEL[info.site] || info.site} ${METRIC_LABEL[info.metric] || info.metric} ${number(info.value)}, top ${info.top}% of ${number(info.n)} ${scope} posts`;
}
