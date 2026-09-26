import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';
import { control } from './LocalFilters';

export default function TagExplorer({ collectionId, filters, onTag }) {
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [offset, setOffset] = useState(0);
  const query = useQuery({ queryKey: ['tag-explorer', collectionId, filters, search, category, offset], queryFn: async () => (await api.dataset.tags(collectionId, { filters, offset, limit: 100 }, search, category)).data });
  return <div className="p-3 space-y-3">
    <div className="text-sm text-slate-200">Tag explorer <span className="text-xs text-slate-400">Distinct active images in the current filter scope</span></div>
    <div className="flex gap-2"><input aria-label="Search tags" className={control} placeholder="Search tags" value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /><select aria-label="Tag category" className={control} value={category} onChange={e => { setCategory(e.target.value); setOffset(0); }}><option value="">All categories</option>{['artist','character','copyright','species','general','meta'].map(c => <option key={c}>{c}</option>)}</select><span className="text-xs text-slate-400">{query.data?.total || 0} tags</span></div>
    {query.isError && <p className="text-red-400 text-xs">Could not load tags.</p>}
    {query.isFetching && <p className="text-slate-400 text-xs">Loading tags…</p>}
    {['artist','character','copyright','species','general','meta'].map(c => {
      const items = (query.data?.items || []).filter(t => t.category === c);
      return items.length > 0 && <div key={c}><h3 className="mb-1 text-xs text-slate-400 capitalize">{c === 'artist' && (filters.tag_basis || 'effective') === 'effective' ? 'Canonical artist trigger' : c}</h3><div className="flex flex-wrap gap-2">{items.map(t => <span className={control} key={t.tag}><button onClick={() => onTag(t.tag, false)} title="Require this tag in Gallery">{t.tag} <span className="text-blue-400">{t.count}</span></button> <button aria-label={`Exclude ${t.tag}`} title="Exclude this tag in Gallery" onClick={() => onTag(t.tag, true)}>−</button></span>)}</div></div>;
    })}
    {!query.isFetching && query.data?.total === 0 && <p className="text-xs text-slate-400">No tags match this scope.</p>}
    <div className="flex gap-2"><button className={control} disabled={!offset} onClick={() => setOffset(Math.max(0, offset-100))}>Previous tags</button><button className={control} disabled={!query.data?.next_cursor} onClick={() => setOffset(Number(query.data.next_cursor))}>Next tags</button></div>
  </div>;
}
