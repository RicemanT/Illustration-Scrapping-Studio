import React from 'react';
import { LEVEL_CLASS, flagInfo } from './analysisFlags';

const SCORER_LABELS = { ws3: 'waifu-scorer v3', ws4: 'waifu-scorer v4', naflex: 'Naflex', aps25: 'aesthetic-predictor 2.5', dbaes: 'DeepGHS dbaesthetic' };
const CLASS_LABELS = {
  polished: 'polished', rough: 'sketch/rough', monochrome: 'monochrome', mono: 'monochrome (detector)', cls_illustration: 'illustration',
  cls_comic: 'comic page', cls_3d: '3D', cls_bangumi: 'anime screenshot', real: 'photo', ai: 'AI-generated',
};
const REJECT = {
  off_style: 'off-style', near_duplicate: 'near-duplicate of a better-scored post', low_aesthetic: "among the dataset's weakest",
  not_analyzed: 'not analysed',
};
const top = (percentile) => `top ${Math.max(0.1, 100 * (1 - percentile)).toFixed(1)}%`;

// What the image analysis found for one image (from the folder review).
export default function AnalysisDetails({ detail, review }) {
  if (!detail) return null;
  const flags = detail.flags || [];
  return (
    <div className="space-y-2 rounded-lg border border-[#202a34] bg-[#0a0f15] p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-100">Image analysis</h3>
        {review?.era && <span className="text-[11px] text-slate-400">artist style: {review.era === 'latest' ? 'latest' : 'main career'}</span>}
      </div>
      {flags.length > 0 ? <div className="flex flex-wrap gap-1">{flags.map((flag) => { const info = flagInfo(flag); return <span key={flag} className={`rounded px-1.5 py-0.5 ${LEVEL_CLASS[info.level]}`}>{info.label}</span>; })}</div>
        : <p className="text-emerald-300">No flags.</p>}
      {detail.would_reject && <p className="text-amber-200">The plan would drop this image: {REJECT[detail.would_reject] || detail.would_reject.replace('content_', '').replaceAll('_', ' ')}.</p>}
      {detail.z != null && <p className="text-slate-300">
        Style distance <b>{detail.z.toFixed(2)}</b> <span className="text-slate-500">(0 = the artist's typical style; flagged above about 1.8, dropped above 2.5)</span> · fit {Math.round(100 * detail.style)}%</p>}
      {detail.ensemble != null && <p className="text-slate-300">
        Aesthetic <b>{top(detail.ensemble)}</b> of the analysed dataset · {Math.round(100 * (detail.aesthetic_rank ?? 0))}th percentile within the artist</p>}
      {detail.scores && <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-slate-400">
        {Object.entries(detail.scores).map(([name, value]) => (
          <span key={name} className="flex justify-between gap-2"><span>{SCORER_LABELS[name] || name}</span>
            <span className="tabular-nums text-slate-300">{Number(value).toFixed(2)}{detail.percentiles?.[name] != null ? ` · ${top(detail.percentiles[name])}` : ''}</span></span>))}
      </div>}
      {detail.classes && <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-slate-400">
        {Object.entries(detail.classes).filter(([, value]) => value >= 0.05).sort((a, b) => b[1] - a[1]).map(([name, value]) => (
          <span key={name}>{CLASS_LABELS[name] || name} <span className="tabular-nums text-slate-300">{Math.round(100 * value)}%</span></span>))}
        {detail.era && <span>drawing style era <span className="text-slate-300">{detail.era}</span></span>}
      </div>}
    </div>
  );
}
