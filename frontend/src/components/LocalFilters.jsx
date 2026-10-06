import React, { useState } from 'react';

export const control = 'rounded border border-[#202a34] bg-[#090d12] px-2 py-1 text-xs text-slate-300';

export default function LocalFilters({ filters, onChange, providers = [], total = 0, qualityFilter = false }) {
  const [tag, setTag] = useState('');
  const update = (key, value) => onChange({ ...filters, [key]: value });
  const add = (key) => {
    const value = tag.replaceAll('_', ' ').trim().replace(/\s+/g, ' ');
    if (value) update(key, [...new Set([...(filters[key] || []), value])]);
    setTag('');
  };
  return <div className="p-3 border-b border-[#202a34] space-y-2">
    <div className="text-xs text-slate-400">Local view filters · {total} matching images. Stored dimensions; future imports are unaffected.</div>
    <div className="flex flex-wrap gap-2">
      <select aria-label="Tag basis" className={control} value={filters.tag_basis || 'effective'} onChange={e => update('tag_basis', e.target.value)}><option value="effective">Effective ground truth</option><option value="provenance">Imported provenance</option></select>
      <input aria-label="Filter tag" className={control} value={tag} onChange={e => setTag(e.target.value)} placeholder="Tag with spaces" onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add('required_tags'); } }} />
      <button className={control} onClick={() => add('required_tags')}>Require tag</button><button className={control} onClick={() => add('excluded_tags')}>Exclude tag</button>
      <select aria-label="Provider filter" className={control} value={filters.provider || ''} onChange={e => update('provider', e.target.value || null)}><option value="">All providers</option>{providers.map(p => <option key={p} value={p}>{p}</option>)}</select>
      <select aria-label="Rating filter" className={control} value={filters.rating || ''} onChange={e => update('rating', e.target.value || null)}><option value="">All ratings</option>{['safe','questionable','explicit','unknown'].map(p => <option key={p}>{p}</option>)}</select>
      <select aria-label="Review filter" className={control} value={filters.review_status || ''} onChange={e => update('review_status', e.target.value || null)}><option value="">All review states</option>{['pending','accepted','rejected','archived'].map(p => <option key={p}>{p}</option>)}</select>
      <select aria-label="Favorite filter" className={control} value={filters.favorite == null ? '' : String(filters.favorite)} onChange={e => update('favorite', e.target.value === '' ? null : e.target.value === 'true')}><option value="">All favorites</option><option value="true">Favorites only</option><option value="false">Not favorited</option></select>
      {qualityFilter && <select aria-label="Quality filter" className={control} value={filters.quality || ''} onChange={e => update('quality', e.target.value || null)}><option value="">All quality marks</option><option value="unviewed">Not viewed yet</option><option value="normal">Normal</option><option value="auto">Auto-assigned</option><option value="manual">Set by hand</option>{['masterpiece','best quality','low quality','very aesthetic','aesthetic'].map(p => <option key={p}>{p}</option>)}</select>}
      <label className="text-xs text-slate-400">posted from <input aria-label="Posted from year" type="number" min="1990" max="2100" placeholder="year" className={`${control} w-20`} value={filters.posted_from_year || ''} onChange={e => update('posted_from_year', e.target.value ? Number(e.target.value) : null)} /></label>
      <label className="text-xs text-slate-400">to <input aria-label="Posted to year" type="number" min="1990" max="2100" placeholder="year" className={`${control} w-20`} value={filters.posted_to_year || ''} onChange={e => update('posted_to_year', e.target.value ? Number(e.target.value) : null)} /></label>
      <select aria-label="Caption filter" className={control} value={filters.caption || ''} onChange={e => update('caption', e.target.value || null)}><option value="">All captions</option><option value="has">Has a caption file</option><option value="missing">No caption file</option></select>
      {['min_width','max_width','min_height','max_height'].map(key => <label key={key} className="text-xs text-slate-400">{key.replace('_',' ')} <input aria-label={key.replace('_',' ')} type="number" min="1" className={`${control} w-20`} value={filters[key] || ''} onChange={e => update(key, e.target.value ? Number(e.target.value) : null)} /></label>)}
      <button className={control} onClick={() => onChange({})}>Clear filters</button>
    </div>
    <div className="flex flex-wrap gap-1">{['required_tags','excluded_tags'].flatMap(key => (filters[key] || []).map(value => <button className={control} key={`${key}-${value}`} onClick={() => update(key, filters[key].filter(t => t !== value))}>{key === 'excluded_tags' ? 'NOT ' : 'AND '}{value} ×</button>))}{filters.image_ids && <button className={control} onClick={() => { const next = { ...filters }; delete next.image_ids; onChange(next); }}>QA selection ({filters.image_ids.length}) ×</button>}</div>
    {filters.rating === 'unknown' && <p className="text-xs text-slate-400">Unknown means a latest source is unrated, or the image has no source. No rating is inferred from pixels.</p>}
  </div>;
}
