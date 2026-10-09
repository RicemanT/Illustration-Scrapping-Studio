import React, { useEffect, useRef, useState } from 'react';

// The picture area of the touch viewers: swipe left/right to move, down to close, up for `upLabel`'s action
// (when given); pinch or double-tap to zoom, drag to pan while zoomed. Shared by the image and wildcard viewers.
export default function SwipeZoomStage({ resetKey, onSwipe, upLabel = null, children }) {
  const [zoom, setZoom] = useState({ scale: 1, x: 0, y: 0 });
  const [drag, setDrag] = useState({ x: 0, y: 0 });
  const pointers = useRef(new Map());
  const gesture = useRef(null);
  const lastTap = useRef({ time: 0, x: 0, y: 0 });
  useEffect(() => { setZoom({ scale: 1, x: 0, y: 0 }); setDrag({ x: 0, y: 0 }); }, [resetKey]);

  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const down = (event) => {
    event.currentTarget.setPointerCapture?.(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = [...pointers.current.values()];
    if (points.length === 2) gesture.current = { type: 'pinch', start: distance(points[0], points[1]), scale: zoom.scale, x: zoom.x, y: zoom.y };
    else if (points.length === 1) gesture.current = { type: zoom.scale > 1 ? 'pan' : 'swipe', sx: event.clientX, sy: event.clientY, x: zoom.x, y: zoom.y, t: Date.now() };
  };
  const move = (event) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const g = gesture.current;
    if (!g) return;
    const points = [...pointers.current.values()];
    if (g.type === 'pinch' && points.length === 2) {
      const scale = Math.min(5, Math.max(1, g.scale * distance(points[0], points[1]) / g.start));
      setZoom({ scale, x: scale === 1 ? 0 : g.x, y: scale === 1 ? 0 : g.y });
    } else if (g.type === 'pan') {
      setZoom((z) => ({ ...z, x: g.x + event.clientX - g.sx, y: g.y + event.clientY - g.sy }));
    } else if (g.type === 'swipe') {
      setDrag({ x: event.clientX - g.sx, y: event.clientY - g.sy });
    }
  };
  const up = (event) => {
    pointers.current.delete(event.pointerId);
    const g = gesture.current;
    if (pointers.current.size > 0) { // one finger of a pinch lifted: continue as a pan
      const [rest] = pointers.current.values();
      gesture.current = { type: zoom.scale > 1 ? 'pan' : 'none', sx: rest.x, sy: rest.y, x: zoom.x, y: zoom.y };
      return;
    }
    gesture.current = null;
    if (!g || g.type !== 'swipe') return;
    const dx = event.clientX - g.sx;
    const dy = event.clientY - g.sy;
    setDrag({ x: 0, y: 0 });
    if (Math.abs(dx) < 12 && Math.abs(dy) < 12 && Date.now() - g.t < 300) {
      const now = Date.now();
      const last = lastTap.current;
      if (now - last.time < 300 && Math.hypot(event.clientX - last.x, event.clientY - last.y) < 30) {
        lastTap.current = { time: 0, x: 0, y: 0 };
        const rect = event.currentTarget.getBoundingClientRect();
        const cx = event.clientX - rect.left - rect.width / 2;
        const cy = event.clientY - rect.top - rect.height / 2;
        setZoom((z) => (z.scale > 1 ? { scale: 1, x: 0, y: 0 } : { scale: 2.5, x: -cx * 1.5, y: -cy * 1.5 }));
      } else {
        lastTap.current = { time: now, x: event.clientX, y: event.clientY };
      }
      return;
    }
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) onSwipe(dx < 0 ? 'left' : 'right');
    else if (dy < -90 && Math.abs(dy) > Math.abs(dx) && upLabel) onSwipe('up');
    else if (dy > 110 && Math.abs(dy) > Math.abs(dx)) onSwipe('down');
  };

  const style = zoom.scale > 1
    ? { transform: `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})` }
    : { transform: `translate(${drag.x}px, ${upLabel ? drag.y : Math.max(drag.y, 0)}px)`, opacity: 1 - Math.min(0.5, Math.abs(drag.y) / 400) };
  return (
    <div className="relative flex-1 touch-none select-none overflow-hidden" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
      <div className="flex h-full w-full items-center justify-center transition-transform duration-75" style={style}>{children}</div>
      {upLabel && drag.y < -40 && <div className="pointer-events-none absolute inset-x-0 top-4 text-center text-sm font-semibold text-red-300">{upLabel}</div>}
    </div>
  );
}
