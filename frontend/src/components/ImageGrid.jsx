import React, { useState, useEffect, useRef } from 'react';
import { justifiedRows } from './justifiedLayout';
import ImageDetail from './ImageDetail';
import { backendAssetUrl } from '../api/client';
import { markFor, markTags, marksOf } from './qualityMarks';
import useDragSelect from './useDragSelect';

function ImageGrid({ images, selected = new Set(), onToggle, onSelectRange, onDragSelect, targetHeight = 220, marking = null, onViewerClose, eraFrom = null }) {
  const [selectedImage, setSelectedImage] = useState(null);
  // Shift+click selects every image between the last clicked checkbox and this one.
  const anchor = useRef(null);
  const toggle = (event, index) => {
    const from = anchor.current;
    if (event.shiftKey && onSelectRange && from !== null && from < images.length) {
      const [start, end] = from < index ? [from, index] : [index, from];
      onSelectRange(images.slice(start, end + 1).map((image) => image.id));
    } else {
      onToggle(images[index].id);
    }
    anchor.current = index;
  };
  useEffect(() => { anchor.current = null; }, [images]);

  const container = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  // Drag a box across tiles (left or right button) to select them; Alt/Ctrl deselects.
  const { box, hits } = useDragSelect(container, (keys, add) => onDragSelect?.(keys.map(Number), add), Boolean(onDragSelect));
  const currentIndex = images.findIndex(image => image.id === selectedImage?.id);
  const rows = justifiedRows(images, width, targetHeight);


  return (
    <>
      <div className="p-2">
        <div ref={container} className="relative select-none space-y-1.5" data-testid="justified-gallery">
          {rows.map((row, rowIndex) => <div key={rowIndex} className="flex gap-1.5" style={{ height: row.height }}>
          {row.items.map(({ image, width: tileWidth }) => {
            const index = images.indexOf(image);
            const year = image.posted_at && /^\d{4}/.test(image.posted_at) ? Number(image.posted_at.slice(0, 4)) : null;
            const outsideEra = Boolean(eraFrom && year && year < eraFrom);
            const isOriginalFrame = ['original_frame', 'archive_frame'].includes(image.derived_media_source);
            const isArchiveFrame = image.derived_media_source === 'archive_frame';
            return (
            <div
              key={image.id}
              data-select-key={image.id}
              onClick={(event) => event.shiftKey && onToggle ? toggle(event, index) : setSelectedImage(image)}
              role="button" tabIndex={0} aria-label={`Open image ${image.id}`}
              onKeyDown={(event) => { if (event.target !== event.currentTarget) return; if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); setSelectedImage(image); } }}
              style={{ width: tileWidth, height: row.height, flexShrink: 0 }}
              className="relative bg-[#0c1219] rounded overflow-hidden cursor-pointer hover:ring-2 hover:ring-blue-400"
            >
              <img
                src={image.thumb_path ? backendAssetUrl(`/static/thumbnails/${image.thumb_path}`) : undefined}
                alt={`Image ${image.id}`}
                draggable={false}
                className={`w-full h-full object-contain transition-opacity ${outsideEra ? 'opacity-35 hover:opacity-100' : ''}`}
                loading="lazy"
              />
              {onToggle && <button type="button" aria-label={`${selected.has(image.id) ? 'Deselect' : 'Select'} image`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); toggle(event, index); }} className={`absolute top-2 left-2 z-10 h-4 w-4 rounded border ${selected.has(image.id) ? 'bg-blue-500 border-blue-300' : 'bg-black/40 border-white/60'}`} />}
              {selected.has(image.id) && <div className="pointer-events-none absolute inset-0 ring-2 ring-inset ring-blue-400 " />}
              {hits.has(String(image.id)) && <div className={`pointer-events-none absolute inset-0 ring-2 ring-inset ${box?.remove ? 'bg-rose-500/15 ring-rose-400' : 'bg-sky-400/15 ring-sky-300'}`} />}
              {Boolean(image.derived_media_source || image.derived_from_preview) && <div title={isArchiveFrame ? `Frame extracted from original ${image.original_media_format || 'archive'}, then normalized for training` : isOriginalFrame ? `Frame extracted from original ${image.original_media_format || 'media'}, then normalized for training` : `Provider preview derived from ${image.original_media_format || 'unsupported media'}, then normalized for training`} className={`absolute right-2 top-2 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ring-1 ${isOriginalFrame ? 'bg-emerald-950/90 text-emerald-200 ring-emerald-700/70' : 'bg-amber-950/90 text-amber-200 ring-amber-700/70'}`}>{isArchiveFrame ? 'archive frame' : isOriginalFrame ? 'original frame' : 'preview still'}</div>}
              <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/60 to-transparent p-2">
                <div className="text-white text-xs" title={image.posted_at ? `Posted ${new Date(image.posted_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}` : 'Posting date unknown'}>
                  {image.width}x{image.height}{year ? <span className={outsideEra ? 'text-amber-300' : 'text-slate-300'}> · {year}</span> : null}
                </div>
              </div>
              <div className="pointer-events-none absolute bottom-2 right-2 flex gap-1 text-[10px]">
                {marking && markTags(marksOf(image)).map(tag => {
                  const automatic = image.quality_source === 'auto' && tag === image.quality_mark;
                  return <span key={tag} title={automatic ? 'Set automatically from the post score' : undefined} className={`rounded bg-black/80 px-1 ${markFor(tag).badge} ${automatic ? 'italic opacity-80' : ''}`}>{tag}{automatic ? ' · auto' : ''}</span>;
                })}
                {outsideEra && <span title={`Posted before the folder's era (${eraFrom})`} className="rounded bg-black/80 px-1 text-amber-300">pre-{eraFrom}</span>}
                {Boolean(image.has_caption) && <span title="Has a caption file" className="rounded bg-black/80 px-1 text-emerald-200">NL</span>}
                {Boolean(image.favorite) && <span title="Favorite" aria-label="Favorite" className="rounded bg-black/80 px-1 text-amber-200">★</span>}
                {image.review_status && image.review_status !== 'pending' && <span className="rounded bg-black/80 px-1 text-slate-200">{image.review_status}</span>}
              </div>
            </div>
            );
          })}</div>)}
          {box && <div aria-hidden="true" className={`pointer-events-none absolute z-20 rounded-sm border ${box.remove ? 'border-rose-400 bg-rose-400/10' : 'border-sky-300 bg-sky-300/10'}`}
            style={{ left: box.x, top: box.y, width: box.w, height: box.h, margin: 0 }} />}
        </div>
      </div>

      {selectedImage && (
        <ImageDetail
          key={selectedImage.id}
          image={selectedImage}
          position={currentIndex + 1} total={images.length}
          onPrevious={currentIndex > 0 ? () => setSelectedImage(images[currentIndex - 1]) : undefined}
          onNext={currentIndex >= 0 && currentIndex < images.length - 1 ? () => setSelectedImage(images[currentIndex + 1]) : undefined}
          onClose={() => { setSelectedImage(null); onViewerClose?.(); }}
          marking={marking}
        />
      )}
    </>
  );
}

export default ImageGrid;
