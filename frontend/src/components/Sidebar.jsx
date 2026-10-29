import React, { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import api from '../api/client';
import { FOLDER_COUNT_REFRESH_MS } from '../api/folderCache';
import CreateCollectionModal from './CreateCollectionModal';
import MergeGroupsModal from './MergeGroupsModal';

function Sidebar({ variant = 'column' }) {
  const drawer = variant === 'drawer';
  const location = useLocation();
  const [showCreateModal, setShowCreateModal] = useState(false);
  // Drag a group heading onto another to merge them.
  const [dragGroup, setDragGroup] = useState(null);
  const [dropTarget, setDropTarget] = useState(null);
  const [merging, setMerging] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [hideAccepted, setHideAccepted] = useState(() => { try { return localStorage.getItem('artist.hideAccepted') === 'true'; } catch { return false; } });
  const toggleHideAccepted = (value) => { setHideAccepted(value); try { localStorage.setItem('artist.hideAccepted', String(value)); } catch { /* storage unavailable */ } };
  // Review state of planner collections: accepted folders get a check mark.
  const review = useQuery({ queryKey: ['tracker-folders'], queryFn: async () => (await api.tracker.folders()).data, refetchInterval: 30000 });
  const reviewState = new Map((review.data?.items || []).map((item) => [item.folder_id, item]));

  const { data: collectionsData, isLoading } = useQuery({
    queryKey: ['collections'],
    // This stays mounted across routes and also catches scheduled jobs,
    // imports completed off-page, and changes from another browser tab.
    refetchInterval: FOLDER_COUNT_REFRESH_MS,
    queryFn: async () => {
      const response = await api.folders.list();
      return response.data;
    },
  });

  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data, refetchInterval: FOLDER_COUNT_REFRESH_MS });
  // Caption files beside the images, counted on the server; refreshed while a captioning script is writing them.
  const captionCounts = useQuery({ queryKey: ['caption-counts'], queryFn: async () => (await api.folders.captionCounts()).data, refetchInterval: 30000 });
  const captioned = (folderId) => captionCounts.data?.folders?.[folderId] ?? null;
  const laidOut = (folderId) => captionCounts.data?.layouts?.[folderId] ?? null;
  const captionTitle = `Images with a ${captionCounts.data?.suffix || 'caption'} file`;
  const layoutTitle = 'Images with a _layout.json file (MOSS-Image layout boxes)';
  const collections = collectionsData || [];
  const sections = [...(groups.data || []), { id: null, name: 'Ungrouped' }];
  const filteredCollections = collections.filter((c) =>
    (c.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    c.query.toLowerCase().includes(searchQuery.toLowerCase()))
    && !(hideAccepted && reviewState.get(c.id)?.completed_at && !reviewState.get(c.id)?.late && location.pathname !== `/folder/${c.id}`)
  );

  return (
    <>
      <div className={drawer ? 'artist-sidebar flex h-full w-full flex-col' : 'artist-sidebar w-52 shrink-0 border-r border-[#202a34] flex flex-col'}>
        {/* Logo/Title */}
        <div className="p-4 border-b border-[#202a34]">
          <h2 className="text-sm font-bold tracking-widest text-slate-200 uppercase">Collections</h2>
        </div>

        {/* New Folder Button */}
        <div className="p-4 border-b border-[#202a34]">
          <button
            onClick={() => setShowCreateModal(true)}
            className="w-full px-3 py-2 bg-[#1b2539] text-blue-200 rounded hover:bg-[#2a3a54] text-xs font-medium"
          >
            + New Collection
          </button>
          <Link to="/groups" className="block mt-3 text-xs text-blue-300">Groups / bulk lists</Link>
          <Link to="/planner" className="block mt-1 text-xs text-blue-300">Dataset planner</Link>
        </div>

        {/* Search */}
        <div className="p-4 border-b border-[#202a34]">
          <input
            type="text"
            placeholder="Search folders..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full px-2 py-1 bg-[#0d1219] border border-[#202a34] rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>

        {review.data?.total > 0 && <label className="flex items-center gap-2 border-b border-[#202a34] px-4 py-2 text-xs text-slate-400">
          <input type="checkbox" checked={hideAccepted} onChange={(e) => toggleHideAccepted(e.target.checked)} />
          Hide accepted ({review.data.accepted}/{review.data.total})
        </label>}
        {review.data?.late_folders > 0 && <div className="border-b border-[#202a34] px-4 py-2 text-xs text-amber-300" title="Accepted folders that received images after you accepted them. Open one to review or remove them.">
          ⚠ {review.data.late_images} late image{review.data.late_images === 1 ? '' : 's'} in {review.data.late_folders} accepted folder{review.data.late_folders === 1 ? '' : 's'}
        </div>}

        {/* Folders List */}
        <div className="flex-1 overflow-y-auto p-4">
          {isLoading && <div className="text-slate-400">Loading...</div>}
          {sections.map((section) => {
            const members = filteredCollections.filter((folder) => folder.group_id === section.id);
            if (searchQuery && !members.length) return null;
            return <details key={section.id ?? 'ungrouped'} open={searchQuery || section.id === null || location.pathname === `/groups/${section.id}` || members.some((folder) => location.pathname === `/folder/${folder.id}`) ? true : undefined} className="mb-3">
              <summary className={`cursor-pointer text-xs font-semibold py-2 rounded ${dropTarget === section.id && dragGroup && dragGroup.id !== section.id ? 'bg-blue-900/50 text-blue-100 outline outline-1 outline-blue-500' : 'text-slate-300'}`}
                draggable={Boolean(section.id)} title={section.id ? 'Drag onto another group to merge them' : undefined}
                onDragStart={(event) => { if (!section.id) return; setDragGroup(section); event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', `group:${section.id}`); }}
                onDragEnd={() => { setDragGroup(null); setDropTarget(null); }}
                onDragOver={(event) => { if (dragGroup && section.id && dragGroup.id !== section.id) { event.preventDefault(); setDropTarget(section.id); } }}
                onDragLeave={() => setDropTarget((current) => (current === section.id ? null : current))}
                onDrop={(event) => { event.preventDefault(); if (dragGroup && section.id && dragGroup.id !== section.id) setMerging({ source: dragGroup, target: section }); setDragGroup(null); setDropTarget(null); }}>{section.name} ({collections.filter((folder) => folder.group_id === section.id).length}){(() => { const accepted = collections.filter((folder) => folder.group_id === section.id && reviewState.get(folder.id)?.completed_at).length; return accepted ? <span className="ml-1 font-normal text-emerald-400" title="Accepted collections">· {accepted} ✓</span> : null; })()}{(() => {
                  const inGroup = collections.filter((folder) => folder.group_id === section.id);
                  const done = inGroup.reduce((sum, folder) => sum + (captioned(folder.id) || 0), 0);
                  const layouts = inGroup.reduce((sum, folder) => sum + (laidOut(folder.id) || 0), 0);
                  const total = inGroup.reduce((sum, folder) => sum + folder.image_count, 0);
                  return done || layouts ? <div className="ml-4 text-[11px] font-normal">
                    <div className={done && done >= total ? 'text-emerald-400' : 'text-sky-300'} title={captionTitle}>{done.toLocaleString()} / {total.toLocaleString()} captioned</div>
                    <div className={layouts && layouts >= total ? 'text-emerald-400' : 'text-violet-300'} title={layoutTitle}>{layouts.toLocaleString()} / {total.toLocaleString()} layout</div>
                  </div> : null; })()}</summary>
              {section.id && <Link to={`/groups/${section.id}`} className="block text-xs text-blue-300 mb-2">Manage / scrape group</Link>}
              <div className="space-y-1">{members.map((collection) => (
                <Link key={collection.id} to={`/folder/${collection.id}`} className={`block px-3 py-2 rounded-lg hover:bg-[#1b2539] ${location.pathname === `/folder/${collection.id}` ? 'bg-[#1b2539] text-blue-200' : 'text-slate-400'}`}>
                  <div className="flex items-center gap-1 font-medium"><span className="truncate">{collection.name}</span>{reviewState.get(collection.id)?.completed_at && <span className="text-emerald-400" title="Accepted">✓</span>}{reviewState.get(collection.id)?.late > 0 && <span className="text-amber-300" title={`${reviewState.get(collection.id).late} image(s) added after acceptance`}>⚠{reviewState.get(collection.id).late}</span>}</div>
                  <div className="text-xs text-slate-400">{collection.image_count} images / {collection.type === 'tag' ? 'query' : collection.type}
                    {captioned(collection.id) > 0 && <span className={captioned(collection.id) >= collection.image_count ? 'text-emerald-400' : 'text-sky-300'}
                      title={captionTitle}> · {captioned(collection.id)} captioned</span>}
                    {laidOut(collection.id) > 0 && <span className={laidOut(collection.id) >= collection.image_count ? 'text-emerald-400' : 'text-violet-300'}
                      title={layoutTitle}> · {laidOut(collection.id)} layout</span>}</div>
                </Link>
              ))}</div>
            </details>;
          })}
        </div>

        {/* Status Footer */}
        <div className="p-4 border-t border-[#202a34] text-sm text-slate-400">
          <div>Total: {collections.length} folders</div>
          <div>Images: {collections.reduce((sum, c) => sum + c.image_count, 0)}</div>
        </div>
      </div>

      {showCreateModal && (
        <CreateCollectionModal onClose={() => setShowCreateModal(false)} />
      )}
      {merging && <MergeGroupsModal source={merging.source} target={merging.target}
        sourceCount={collections.filter((folder) => folder.group_id === merging.source.id).length}
        targetCount={collections.filter((folder) => folder.group_id === merging.target.id).length}
        onClose={() => setMerging(null)} />}
    </>
  );
}

export default Sidebar;
