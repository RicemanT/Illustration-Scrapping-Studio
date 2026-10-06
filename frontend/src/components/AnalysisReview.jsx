import React from 'react';
import { Link } from 'react-router-dom';
import { LEVEL_CLASS, flagInfo } from './analysisFlags';

const CONTENT = { rough: 'sketches', monochrome: 'monochrome', comic: 'comic pages', '3d': '3D', photo: 'photos', ai: 'AI images' };

// Summary of the image analysis for a planner collection, above its gallery.
export default function AnalysisReview({ review, flaggedOnly, onFlaggedOnly, loading }) {
  if (loading && !review) return <div className="border-b border-[#202a34] px-3 py-2 text-xs text-slate-500">Checking image analysis…</div>;
  if (!review) return null;
  if (!review.analysed) {
    return (
      <div className="border-b border-[#202a34] px-3 py-2 text-xs text-slate-500">
        Not analysed yet. <Link to="/planner" className="text-blue-300 underline">Run image analysis</Link> to get style, content and aesthetic flags here.
      </div>
    );
  }
  const counts = Object.entries(review.counts || {}).sort((a, b) => flagInfo(b[0]).level - flagInfo(a[0]).level || b[1] - a[1]);
  const flagged = Object.values(review.images || {}).filter((detail) => detail.flags?.length).length;
  const kept = (review.kept_categories || []).filter((name) => CONTENT[name] && name !== 'photo' && name !== 'ai');
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-[#202a34] px-3 py-2 text-xs">
      <span className="text-slate-400" title={`DINO style models: ${(review.models || []).join(', ') || 'none'}`}>Analysis</span>
      <span className="text-slate-300">{review.judged_style ? (review.era === 'latest' ? 'latest style' : 'main career style (the latest era was too small)') : 'too few samples to judge style'}</span>
      {review.excluded?.length > 0 && <span className="text-slate-500">· excluded: {review.excluded.map((name) => CONTENT[name] || name).join(', ')}</span>}
      {kept.length > 0 && <span className="text-emerald-300" title="Most of this artist's style is like this, so it is kept">· kept as their style: {kept.map((name) => CONTENT[name]).join(', ')}</span>}
      <span className="mx-1 text-slate-600">|</span>
      {counts.length === 0 && <span className="text-emerald-300">No flags</span>}
      {counts.map(([flag, count]) => { const info = flagInfo(flag); return <span key={flag} title={info.label} className={`rounded px-1.5 py-0.5 ${LEVEL_CLASS[info.level]}`}>{info.short} {count}</span>; })}
      {flagged > 0 && <button type="button" onClick={() => onFlaggedOnly(!flaggedOnly)}
        className={`ml-auto rounded border px-2 py-0.5 ${flaggedOnly ? 'border-amber-600 text-amber-200' : 'border-[#2a3644] text-slate-300 hover:bg-[#121a24]'}`}>
        {flaggedOnly ? 'Show all images' : `Show only the ${flagged} flagged`}</button>}
    </div>
  );
}
