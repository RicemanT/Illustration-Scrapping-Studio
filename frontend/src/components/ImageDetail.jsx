import { createPortal } from 'react-dom';
import React, { useEffect, useState, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api, { backendAssetUrl } from '../api/client';
import ImageStorageLocations from './ImageStorageLocations';
import { MARKS, applyMark, isActive, markFor, markTags, marksOf } from './qualityMarks';
import { describeAuto } from './qualityJob';
import CaptionEditor from './CaptionEditor';

// Unambiguous dates ("Mar 2, 2024"), whatever the browser's day/month order.
const formatDate = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
};
const PROVIDER_LABEL = { danbooru: 'Danbooru', gelbooru: 'Gelbooru', e621: 'e621', pixiv: 'Pixiv', deviantart: 'DeviantArt', twitter: 'Twitter / X', artstation: 'ArtStation' };
const parseInfo = (value) => { try { return value ? JSON.parse(value) : null; } catch { return null; } };
const autoOf = (row) => ({ source: row?.quality_source || null, tag: row?.quality_auto || null, info: parseInfo(row?.quality_auto_info) });

// marking: { folderId, locked } for planner collections; enables quality marks.
function ImageDetail({ image, onClose, onPrevious, onNext, position, total, marking = null }) {
  const dialog = useRef(null);
  const [showInfo, setShowInfo] = useState(() => { try { return localStorage.getItem('artist.viewerInfo') !== 'false'; } catch { return true; } });
  const [zoom, setZoom] = useState(false);
  const [imageLoading, setImageLoading] = useState(true);
  const [imageFailed, setImageFailed] = useState(false);
  const { data: fullImage, isLoading: detailsLoading, error: detailsError, refetch } = useQuery({
    queryKey: ['image', image.id],
    queryFn: async () => {
      const response = await api.images.get(image.id);
      return response.data;
    },
  });
  const queryClient = useQueryClient();
  const [notes, setNotes] = useState(image.notes || '');
  const [tagText, setTagText] = useState('');
  const [tagsEdited, setTagsEdited] = useState(false);
  const [tagUndoToken, setTagUndoToken] = useState(null);
  const reviewMutation = useMutation({
    meta: { successMessage: 'Image review saved' },
    mutationFn: (data) => api.images.review(image.id, data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['image', image.id] }); queryClient.invalidateQueries({ queryKey: ['collection-images'] }); queryClient.invalidateQueries({ queryKey: ['tag-explorer'] }); },
  });
  const tagMutation = useMutation({
    meta: { successMessage: 'Ground-truth tags saved' },
    mutationFn: () => api.tags.replaceGroundTruth(
      image.id,
      tagText.split(/[,\n]/).map((tag) => tag.replaceAll('_', ' ').trim()).filter(Boolean),
    ),
    onSuccess: async (response) => {
      setTagUndoToken(response.data.undo_token);
      await queryClient.invalidateQueries({ queryKey: ['image', image.id] });
      setTagsEdited(false);
      queryClient.invalidateQueries({ queryKey: ['collection-images'] });
      queryClient.invalidateQueries({ queryKey: ['tag-explorer'] });
    },
  });
  const undoTagMutation = useMutation({
    meta: { successMessage: 'Tag edit undone' },
    mutationFn: () => api.tags.undo(tagUndoToken),
    onSuccess: () => {
      setTagUndoToken(null);
      queryClient.invalidateQueries({ queryKey: ['image', image.id] });
      queryClient.invalidateQueries({ queryKey: ['collection-images'] });
      queryClient.invalidateQueries({ queryKey: ['tag-explorer'] });
    },
  });
  // Quality marks are saved as working marks right away and written into the
  // ground truth when the folder is accepted.
  const markable = Boolean(marking) && !marking.locked;
  const [marks, setMarks] = useState(() => marksOf(image));
  const marksRef = useRef(marks);
  const marksTouched = useRef(false);
  const saveChain = useRef(Promise.resolve());
  const [markError, setMarkError] = useState(null);
  // Where the quality mark came from (hand or score percentile) and the automatic suggestion.
  const [auto, setAuto] = useState(() => autoOf(image));
  useEffect(() => {
    if (fullImage && !marksTouched.current) { marksRef.current = marksOf(fullImage); setMarks(marksRef.current); setAuto(autoOf(fullImage)); }
  }, [fullImage]);
  const updateImageCaches = (row) => {
    queryClient.setQueryData(['image', image.id], (old) => (old ? { ...old, ...row } : old));
    queryClient.setQueriesData({ queryKey: ['collection-images', String(marking.folderId)] }, (old) => {
      if (!old) return old;
      const patch = (items) => items?.map((item) => (item.id === image.id ? { ...item, ...row } : item));
      return { ...old, items: patch(old.items), images: patch(old.images) };
    });
    queryClient.invalidateQueries({ queryKey: ['planner-folder', String(marking.folderId)] });
  };
  const saveMarks = (next, options) => {
    marksTouched.current = true;
    marksRef.current = next;
    setMarks(next);
    saveChain.current = saveChain.current
      .then(() => api.planner.setMarks(marking.folderId, image.id, { ...next, ...options }))
      .then((response) => { setMarkError(null); setAuto(autoOf(response.data)); updateImageCaches(response.data); })
      .catch((error) => setMarkError(`Mark not saved: ${error.response?.data?.detail || error.message}`));
  };
  const chooseMark = (mark) => {
    if (!markable) return;
    // Normal (no scale) sets both scales by hand; a hand-set quality mark is never replaced automatically.
    const touched = mark.axis ? [mark.axis] : ['quality', 'aesthetic'];
    if (touched.includes('quality')) setAuto((current) => ({ ...current, source: 'manual' }));
    saveMarks(applyMark(mark, marksRef.current), { touched });
  };
  const applyAutoQuality = () => {
    if (!markable) return;
    setAuto((current) => ({ ...current, source: 'auto' }));
    saveMarks({ ...marksRef.current, quality: auto.tag }, { use_auto: true });
  };
  useEffect(() => {
    // Opening an image counts as reviewing it; unmarked images stay normal.
    if (!markable || image.marks_viewed_at) return;
    api.planner.markViewed(marking.folderId, image.id)
      .then((response) => updateImageCaches({ marks_viewed_at: response.data.marks_viewed_at }))
      .catch(() => {});
  }, []);
  const committedTags = (() => { try { return JSON.parse(fullImage?.quality_tags || '[]'); } catch { return []; } })();
  const currentTags = markTags(marks);
  const dirty = tagsEdited && tagText !== (fullImage?.ground_truth_tags || []).join(', ');
  const pending = tagMutation.isPending || reviewMutation.isPending || undoTagMutation.isPending;
  const captionDirty = useRef(false);
  const leave = (action) => {
    if (!action || pending) return;
    const unsaved = [dirty && 'ground-truth tag edits', captionDirty.current && 'caption edits'].filter(Boolean);
    if (unsaved.length && !window.confirm(`Discard unsaved ${unsaved.join(' and ')}?`)) return;
    action();
  };
  const navigation = useRef({});
  navigation.current = { leave, onClose, onPrevious, onNext, chooseMark: markable ? chooseMark : null };
  useEffect(() => { try { localStorage.setItem('artist.viewerInfo', String(showInfo)); } catch {} }, [showInfo]);
  useEffect(() => {
    const previous = document.activeElement;
    const app = document.querySelector('[data-app-shell]');
    if (app) app.inert = true;
    dialog.current?.focus();
    const handleKey = (event) => {
      const { leave, onClose, onPrevious, onNext } = navigation.current;
      if (event.key === 'Escape') { event.preventDefault(); leave(onClose); }
      if (!['INPUT','TEXTAREA','SELECT'].includes(event.target?.tagName)) {
        if (event.key === 'ArrowLeft') { event.preventDefault(); leave(onPrevious); }
        if (event.key === 'ArrowRight') { event.preventDefault(); leave(onNext); }
        const mark = !event.ctrlKey && !event.metaKey && !event.altKey && !event.repeat && MARKS.find((item) => item.key === event.key.toLowerCase());
        if (mark && navigation.current.chooseMark) { event.preventDefault(); navigation.current.chooseMark(mark); }
      }
      if (event.key === 'Tab') {
        const elements = [...dialog.current.querySelectorAll('button:not(:disabled),input,textarea,select,a[href],[tabindex="0"]')].filter(e => e.getClientRects().length);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first?.focus(); }
      }
    };
    window.addEventListener('keydown', handleKey, true);
    return () => { window.removeEventListener('keydown', handleKey, true); if (app) app.inert = false; if (previous?.isConnected) previous.focus(); };
  }, []);

  const displayImage = fullImage || image;
  const isOriginalFrame = ['original_frame', 'archive_frame'].includes(displayImage.derived_media_source);
  const isArchiveFrame = displayImage.derived_media_source === 'archive_frame';

  useEffect(() => { setNotes(displayImage.notes || ''); }, [displayImage.notes]);
  const savedTags = (displayImage.ground_truth_tags || []).join(', ');
  useEffect(() => { if (!tagsEdited) setTagText(savedTags); }, [savedTags, tagsEdited]);

  return createPortal(
    <div ref={dialog} role="dialog" aria-modal="true" aria-label="Image viewer" tabIndex={-1} className="viewer-atmosphere fixed inset-0 z-50 flex flex-col outline-none">
      <header className="flex flex-wrap items-center gap-3 border-b border-slate-700 px-4 py-2 text-sm">
        <span className="mr-auto">Image {image.id}{total ? ` — ${position} of ${total} on this page` : ''}</span>
        <button aria-label="Previous image" disabled={!onPrevious || pending} onClick={() => leave(onPrevious)} className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40">Previous</button>
        <button aria-label="Next image" disabled={!onNext || pending} onClick={() => leave(onNext)} className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40">Next</button>
        <button onClick={() => setZoom(value => !value)} className="rounded border border-slate-700 px-3 py-1">{zoom ? 'Fit image' : 'Actual size'}</button>
        <button aria-expanded={showInfo} onClick={() => setShowInfo(value => !value)} className="rounded border border-slate-700 px-3 py-1">{showInfo ? 'Hide information' : 'Show information'}</button>
        <button aria-label="Close image viewer" onClick={() => leave(onClose)} disabled={pending} className="rounded border border-slate-700 px-3 py-1">Close</button>
      </header>
      <div className="flex flex-1 min-h-0 flex-col md:flex-row">
      <div className={`relative flex-1 min-w-0 min-h-0 overflow-auto p-3 ${zoom ? '' : 'flex items-center justify-center'}`}>
        {imageLoading && !imageFailed && <span role="status" className="absolute top-3 left-3 rounded bg-black/70 p-2 text-xs">Loading image...</span>}
        {imageFailed && <p role="alert">Image unavailable. Check File locations or run Dataset QA.</p>}
        <img src={backendAssetUrl(`/static/images/${displayImage.path}`)} alt={`Image ${displayImage.id}`}
          onLoad={() => setImageLoading(false)} onError={() => { setImageLoading(false); setImageFailed(true); }}
          style={zoom ? { width: displayImage.width, maxWidth: 'none', height: 'auto' } : undefined}
          className={imageFailed ? 'hidden' : zoom ? 'block' : 'max-w-full max-h-full object-contain'} />
      </div>
      <div hidden={!showInfo} className="w-full md:w-[380px] lg:w-[440px] max-h-[50vh] md:max-h-none viewer-info shrink-0 overflow-y-auto border-l border-slate-700">
        {detailsLoading && <p role="status" className="p-3 text-sm">Loading metadata...</p>}
        {detailsError && <p role="alert" className="p-3 text-red-300">Metadata unavailable. <button onClick={() => refetch()}>Retry</button></p>}
        <div className="p-6 space-y-6">
          {/* Basic Info */}
          <div>
            <h2 className="text-xl font-bold text-slate-100 mb-4">Image Details</h2>
            {Boolean(displayImage.derived_media_source || displayImage.derived_from_preview) && (
              <div className={`mb-4 rounded border p-3 text-sm ${isOriginalFrame ? 'border-emerald-800 bg-emerald-950 text-emerald-200' : 'border-amber-800 bg-amber-950 text-amber-200'}`}>
                <div className="font-semibold">{isArchiveFrame ? 'Frame from original archive' : isOriginalFrame ? 'Frame from original media' : 'Provider preview frame'}</div>
                <div className="mt-1 text-xs">{isArchiveFrame ? `Extracted from the naturally ordered middle of the original ${displayImage.original_media_format || 'archive'}, then saved using the processing policy active at import.` : isOriginalFrame ? `Extracted from the original ${displayImage.original_media_format || 'media'} file, then saved using the processing policy active at import.` : `Derived from a ${displayImage.original_media_format || 'non-image'} original. This is the provider thumbnail, normalized for training rather than decoded from the original media.`}</div>
                {displayImage.original_media_url && <a href={displayImage.original_media_url} target="_blank" rel="noopener noreferrer" className="mt-1 block break-all text-xs text-amber-200 underline">Original media URL</a>}
              </div>
            )}
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-slate-400">Dimensions:</span>
                <span className="font-medium">{displayImage.width} x {displayImage.height}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Format:</span>
                <span className="font-medium uppercase">{displayImage.format}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Size:</span>
                <span className="font-medium">{(displayImage.file_size / 1024 / 1024).toFixed(2)} MB</span>
              </div>
              <div className="flex justify-between gap-3">
                <span className="text-slate-400">Posted:</span>
                <span className="text-right font-medium" title={displayImage.posted_at ? `Originally published ${new Date(displayImage.posted_at).toLocaleString()}` : 'The source metadata has no publication date'}>
                  {displayImage.posted_at ? `${formatDate(displayImage.posted_at)}${displayImage.posted_on ? ` on ${PROVIDER_LABEL[displayImage.posted_on] || displayImage.posted_on}` : ''}` : (detailsLoading ? '…' : 'unknown')}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Added to library:</span>
                <span className="font-medium" title={new Date(displayImage.added_at).toLocaleString()}>{formatDate(displayImage.added_at)}</span>
              </div>
            </div>
          </div>

          {marking && (
            <div className="rounded-lg border border-[#202a34] bg-[#0a0f15] p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-slate-100">Quality</h3>
                <span className="text-[11px] text-slate-400">{marking.locked ? 'Folder accepted · reopen it to change' : 'Q W E quality · A S aesthetic · D normal'}</span>
              </div>
              <div className="grid grid-flow-col grid-cols-2 grid-rows-3 gap-1.5" role="group" aria-label="Quality marks">
                {MARKS.map((mark) => {
                  const on = isActive(mark, marks);
                  return (
                    <button key={mark.key} type="button" aria-pressed={on} disabled={!markable} onClick={() => chooseMark(mark)}
                      title={mark.axis ? `${mark.label} (${mark.key.toUpperCase()}); press again to clear` : `Normal (${mark.key.toUpperCase()}): no quality tags`}
                      className={`flex items-center gap-2 rounded-md border px-2.5 py-2 text-left text-xs font-medium transition-all duration-150 disabled:cursor-not-allowed ${on ? mark.active : `border-[#26313d] bg-[#0d141c] text-slate-300 ${markable ? mark.hover : 'opacity-60'}`}`}>
                      <span className={`h-2 w-2 shrink-0 rounded-full ${mark.dot} ${on ? '' : 'opacity-40'}`} />
                      <span className="flex-1 truncate">{mark.label}</span>
                      {on && mark.axis === 'quality' && auto.source === 'auto' && <span className="rounded bg-black/30 px-1 text-[9px] uppercase tracking-wide opacity-80" title="Set automatically from the post's score">auto</span>}
                      <kbd className={`rounded border px-1.5 font-mono text-[10px] uppercase ${on ? 'border-white/30 bg-black/30' : 'border-[#2b3744] bg-black/30 text-slate-400'}`}>{mark.key}</kbd>
                    </button>
                  );
                })}
              </div>
              <p className="mt-2 text-[11px] text-slate-400">
                {marking.locked ? 'Written to the ground truth when the folder was accepted.'
                  : `Saved as you mark; added to the ground truth when you accept the folder.${committedTags.join(', ') !== currentTags.join(', ') && committedTags.length ? ` The sidecar still has: ${committedTags.join(', ')}.` : ''}`}
              </p>
              {auto.info && (
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                  <span>{describeAuto(auto.tag, auto.info)}</span>
                  {markable && auto.source === 'manual' && !auto.info.reason && (auto.tag || null) !== (marks.quality || null) && (
                    <button type="button" onClick={applyAutoQuality} className="rounded border border-slate-600 px-1.5 py-0.5 text-slate-200 hover:border-slate-400">Use auto</button>
                  )}
                </div>
              )}
              {markError && <p role="alert" className="mt-1 text-xs text-red-300">{markError}</p>}
            </div>
          )}

          <ImageStorageLocations locations={displayImage.storage_locations} />

          {/* Folder */}
          {(displayImage.folders || displayImage.collections)?.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-slate-100 mb-2">Folder</h3>
              <div className="flex flex-wrap gap-2">
                {(displayImage.folders || displayImage.collections).map((collectionName, idx) => (
                  <span
                    key={idx}
                    className="px-2 py-1 bg-blue-950 text-blue-200 rounded text-xs"
                  >
                    {collectionName}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Tags */}
          <div className="border-t border-[#202a34] pt-4">
            <h3 className="text-sm font-semibold text-slate-100 mb-2">Review</h3>
            <div className="flex gap-2 items-center">
              <select value={displayImage.review_status || 'pending'} disabled={pending || !fullImage} onChange={(e) => reviewMutation.mutate({ review_status: e.target.value })} className="border border-[#303d4c] rounded px-2 py-1 text-sm">
                <option value="pending">Pending</option><option value="accepted">Accepted</option><option value="rejected">Rejected</option><option value="archived">Archived</option>
              </select>
              <label className="text-sm"><input type="checkbox" checked={Boolean(displayImage.favorite)} disabled={pending || !fullImage} onChange={(e) => reviewMutation.mutate({ favorite: e.target.checked })} /> Favorite</label>
            </div>
            <textarea disabled={!fullImage || pending} value={notes} onChange={(e) => setNotes(e.target.value)} onBlur={() => { if (notes !== displayImage.notes && notes !== (displayImage.notes || '')) reviewMutation.mutate({ notes }); }} placeholder="Notes" className="mt-2 w-full border border-[#303d4c] rounded p-2 text-sm" rows={3} />
          </div>

          {/* Editable ground-truth tags */}
          <div className="border-t border-[#202a34] pt-4">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-100">Ground-truth tags</h3>
              <span className="text-xs text-slate-400">Written to sidecar</span>
            </div>
            <textarea
              disabled={!fullImage || pending}
              value={tagText}
              onChange={(event) => { setTagsEdited(true); setTagText(event.target.value.replaceAll('_', ' ')); }}
              rows={6}
              placeholder="1girl, solo, blue hair"
              className="w-full rounded border border-[#303d4c] p-2 text-sm text-slate-100"
            />
            <div className="mt-2 flex items-center gap-2">
              <button type="button" onClick={() => tagMutation.mutate()} disabled={pending || !fullImage} className="rounded bg-[#344a73] px-3 py-1.5 text-xs text-white disabled:opacity-50">{tagMutation.isPending ? 'Saving…' : 'Save ground truth'}</button>
              {tagUndoToken && <button type="button" onClick={() => undoTagMutation.mutate()} disabled={undoTagMutation.isPending} className="rounded border border-emerald-600 px-3 py-1.5 text-xs text-emerald-300 disabled:opacity-50">Undo tag edit</button>}
            </div>
            {tagMutation.isError && <div className="mt-2 text-xs text-red-300">{tagMutation.error.response?.data?.detail || tagMutation.error.message}</div>}
          </div>

          {/* Natural-language caption file beside the image */}
          <CaptionEditor imageId={image.id} dirtyRef={captionDirty}
            onSavedNext={onNext ? () => navigation.current.leave(navigation.current.onNext) : undefined} />

          {marking && (
            <div>
              <h3 className="text-sm font-semibold text-slate-100 mb-1">Quality tags</h3>
              <p className="mb-2 text-xs text-slate-400">{marking.locked ? 'In the ground truth, after all other tags' : 'Go after all other tags when the folder is accepted'}</p>
              <div className="flex flex-wrap gap-1">
                {currentTags.length ? currentTags.map((tag) => <span key={tag} className={`rounded bg-[#1c2530] px-2 py-1 text-xs ${markFor(tag).badge}`}>{tag}</span>)
                  : <span className="rounded bg-[#1c2530] px-2 py-1 text-xs text-slate-400">normal (no quality tags)</span>}
              </div>
            </div>
          )}

          {/* Imported source tags */}
          {displayImage.tags && Object.keys(displayImage.tags).length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-slate-100 mb-1">Imported source tags</h3>
              <p className="mb-2 text-xs text-slate-400">Read-only provenance</p>
              <div className="space-y-3">
                {Object.entries(displayImage.tags).map(([category, tags]) => (
                  <div key={category}>
                    <h4 className="text-xs font-medium text-slate-400 uppercase mb-1">
                      {category}
                    </h4>
                    <div className="flex flex-wrap gap-1">
                      {tags.map((tag, idx) => (
                        <span
                          key={idx}
                          className="px-2 py-1 bg-[#1c2530] text-slate-300 rounded text-xs"
                        >
                          {tag.replaceAll('_', ' ')}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Sources */}
          {displayImage.sources && displayImage.sources.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-slate-100 mb-2">Sources</h3>
              <div className="space-y-3">
                {displayImage.sources.map((source, idx) => (
                  <div key={idx} className="border border-[#202a34] rounded-lg p-3">
                    <div className="flex items-center justify-between mb-2">
                      <span className="font-medium text-sm capitalize">{source.provider}</span>
                      <span className="text-right text-xs text-slate-400">
                        {source.posted_at && <span className="block" title={new Date(source.posted_at).toLocaleString()}>Posted {formatDate(source.posted_at)}</span>}
                        <span className="block" title={new Date(source.fetched_at).toLocaleString()}>Fetched {formatDate(source.fetched_at)}</span>
                      </span>
                    </div>
                    {source.remote_url && (
                      <a
                        href={source.remote_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-blue-600 hover:text-blue-800 break-all"
                      >
                        View original →
                      </a>
                    )}
                    {source.metadata && (
                      <details className="mt-2">
                        <summary className="cursor-pointer text-xs text-slate-400">Raw metadata</summary>
                        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-[#11161d] p-2 text-[10px] text-slate-300">
                          {typeof source.metadata === 'string' ? source.metadata : JSON.stringify(source.metadata, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Hashes */}
          <div>
            <h3 className="text-sm font-semibold text-slate-100 mb-2">Hashes</h3>
            <div className="space-y-2 text-xs">
              <div>
                <span className="text-slate-400">SHA256:</span>
                <code className="ml-2 text-slate-100 break-all">{displayImage.sha256}</code>
              </div>
              {displayImage.md5 && (
                <div>
                  <span className="text-slate-400">MD5:</span>
                  <code className="ml-2 text-slate-100">{displayImage.md5}</code>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
      </div>
    </div>, document.body
  );
}

export default ImageDetail;
