import React, { useEffect, useRef, useState } from 'react';
import { justifiedRows } from './justifiedLayout';

export default function ImportGallery({ posts, selected, setSelected, disabled }) {
  const [filter, setFilter] = useState('all');
  const [size, setSize] = useState(160);
  const [width, setWidth] = useState(0);
  const container = useRef(null);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const visible = posts.filter(post => filter === 'all' || (filter === 'have' ? post.already_imported : !post.already_imported));
  const rows = justifiedRows(visible, width, size);
  const toggle = id => setSelected(current => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });
  return <section aria-label="Remote preview gallery" className="space-y-3">
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <label>Show <select aria-label="Import result filter" value={filter} onChange={e => setFilter(e.target.value)} className="rounded border border-slate-700 px-2 py-1"><option value="all">All results</option><option value="new">New only</option><option value="have">Already have</option></select></label>
      <span>{visible.length} shown / {posts.length} results · {selected.size} selected across pages</span>
      <label>Preview size <input aria-label="Import preview size" type="range" min="100" max="280" step="20" value={size} onChange={e => setSize(Number(e.target.value))} className="w-20 align-middle" /></label>
      <button disabled={disabled} onClick={() => setSelected(current => new Set([...current, ...visible.filter(post => !post.already_imported).map(post => post.remote_id)]))}>Select visible new</button>
      <button disabled={disabled || !selected.size} onClick={() => setSelected(new Set())}>Clear selection</button>
    </div>
    <div ref={container} className="space-y-1.5 max-h-[65vh] overflow-y-auto" style={{ scrollbarGutter: 'stable' }}>
      {!visible.length && <p className="p-4 text-sm text-slate-400">No results in this view.</p>}
      {rows.map((row, index) => <div key={index} className="flex gap-1.5" style={{ height: row.height }}>
        {row.items.map(({ image: post, width: tileWidth }) => <button type="button" key={`${post.provider}-${post.remote_id}`} disabled={disabled}
          aria-label={`Select remote image ${post.remote_id}`} aria-pressed={selected.has(post.remote_id)} onClick={() => toggle(post.remote_id)}
          title={`${post.remote_id} · ${post.width} × ${post.height} · ${Object.values(post.tags || {}).flat().slice(0, 12).join(', ')}`}
          style={{ width: tileWidth, height: row.height, flexShrink: 0 }} className="relative overflow-hidden rounded bg-slate-900">
          {post.preview_url ? <img src={post.preview_url} alt={`Remote ${post.remote_id}`} loading="lazy" className="h-full w-full object-contain" /> : <span className="text-xs text-slate-400">No preview</span>}
          <span className={`absolute left-1 top-1 rounded px-1 py-0.5 text-[10px] ${post.already_imported ? 'bg-emerald-950 text-emerald-200' : 'bg-slate-950 text-blue-200'}`}>{post.already_imported ? 'Have' : 'New'}</span>
          {selected.has(post.remote_id) && <span className="pointer-events-none absolute inset-0 ring-2 ring-inset ring-blue-400"><span className="absolute top-1 right-1 rounded bg-[#344a73] px-1 text-xs">✓</span></span>}
          <span className="absolute bottom-0 inset-x-0 bg-black/70 px-1 py-0.5 text-[10px]">{post.width} × {post.height}</span>
        </button>)}
      </div>)}
    </div>
  </section>;
}
