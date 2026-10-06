import { useEffect, useRef, useState } from 'react';

const THRESHOLD = 6; // pixels before a press becomes a drag; shorter presses stay clicks
const EDGE = 56; // pixels from the scroll area's edge where dragging scrolls

function scrollParent(node) {
  for (let element = node?.parentElement; element; element = element.parentElement) {
    const { overflowY } = getComputedStyle(element);
    if (/(auto|scroll)/.test(overflowY) && element.scrollHeight > element.clientHeight) return element;
  }
  return document.scrollingElement || document.documentElement;
}

// Box (marquee) selection over items marked with data-select-key inside `containerRef`.
// Left-drag (touchpad: double-tap and drag) or right-drag selects everything the box touches;
// holding Alt or Ctrl deselects instead. Clicks without movement are left alone, and dragging
// near the top or bottom of the scroll area scrolls it. Esc cancels.
export default function useDragSelect(containerRef, onSelect, enabled = true) {
  const [box, setBox] = useState(null);
  const [hits, setHits] = useState(() => new Set());
  const callback = useRef(onSelect);
  callback.current = onSelect;

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !enabled) return undefined;
    let drag = null;
    let frame = 0;
    let suppressClickUntil = 0;
    let suppressMenuUntil = 0;

    const keysIn = (rect) => {
      const origin = container.getBoundingClientRect();
      const found = new Set();
      for (const element of container.querySelectorAll('[data-select-key]')) {
        const r = element.getBoundingClientRect();
        const left = r.left - origin.left;
        const top = r.top - origin.top;
        if (left < rect.x + rect.w && left + r.width > rect.x && top < rect.y + rect.h && top + r.height > rect.y) found.add(element.dataset.selectKey);
      }
      return found;
    };
    const update = () => {
      if (!drag) return;
      const origin = container.getBoundingClientRect();
      const x = drag.clientX - origin.left;
      const y = drag.clientY - origin.top;
      drag.rect = { x: Math.min(drag.x, x), y: Math.min(drag.y, y), w: Math.abs(x - drag.x), h: Math.abs(y - drag.y) };
      setBox({ ...drag.rect, remove: drag.remove });
      setHits(keysIn(drag.rect));
    };
    const scrollTick = () => {
      if (!drag?.active) return;
      const scroller = drag.scroller;
      const page = scroller === document.scrollingElement || scroller === document.documentElement;
      const bounds = page ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect();
      let delta = 0;
      if (drag.clientY < bounds.top + EDGE) delta = -Math.ceil((bounds.top + EDGE - drag.clientY) / 3);
      else if (drag.clientY > bounds.bottom - EDGE) delta = Math.ceil((drag.clientY - (bounds.bottom - EDGE)) / 3);
      if (delta) {
        const before = scroller.scrollTop;
        scroller.scrollTop += delta;
        if (scroller.scrollTop !== before) update();
      }
      frame = requestAnimationFrame(scrollTick);
    };
    const finish = (event, cancelled = false) => {
      if (!drag || (event && event.pointerId !== drag.pointerId)) return;
      const ended = drag;
      drag = null;
      cancelAnimationFrame(frame);
      document.body.style.userSelect = '';
      if (ended.active) {
        try { container.releasePointerCapture(ended.pointerId); } catch { /* already released */ }
        if (!cancelled && ended.rect) {
          const keys = [...keysIn(ended.rect)];
          if (keys.length) callback.current(keys, !ended.remove);
        }
        suppressClickUntil = Date.now() + 400;
        if (ended.button === 2) suppressMenuUntil = Date.now() + 600;
      }
      setBox(null);
      setHits(new Set());
    };
    const down = (event) => {
      if (event.pointerType === 'touch' || (event.button !== 0 && event.button !== 2) || drag) return;
      if (event.target.closest('input, textarea, select, a, [data-no-drag-select]')) return;
      const origin = container.getBoundingClientRect();
      drag = {
        pointerId: event.pointerId, button: event.button, active: false, rect: null,
        x: event.clientX - origin.left, y: event.clientY - origin.top,
        startX: event.clientX, startY: event.clientY, clientX: event.clientX, clientY: event.clientY,
        remove: event.altKey || event.ctrlKey || event.metaKey, scroller: scrollParent(container),
      };
    };
    const move = (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      drag.clientX = event.clientX;
      drag.clientY = event.clientY;
      drag.remove = event.altKey || event.ctrlKey || event.metaKey;
      if (!drag.active) {
        if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < THRESHOLD) return;
        drag.active = true;
        try { container.setPointerCapture(event.pointerId); } catch { /* pointer already gone */ }
        document.body.style.userSelect = 'none';
        window.getSelection()?.removeAllRanges();
        frame = requestAnimationFrame(scrollTick);
      }
      event.preventDefault();
      update();
    };
    const up = (event) => finish(event);
    const cancel = (event) => finish(event, true);
    const key = (event) => { if (event.key === 'Escape' && drag?.active) { event.preventDefault(); event.stopPropagation(); finish(null, true); } };
    const click = (event) => { if (Date.now() < suppressClickUntil) { event.preventDefault(); event.stopPropagation(); } };
    // Windows opens the context menu after the button is released; elsewhere while it is held.
    const menu = (event) => { if ((drag && drag.button === 2) || Date.now() < suppressMenuUntil) event.preventDefault(); };
    const nativeDrag = (event) => event.preventDefault();

    container.addEventListener('pointerdown', down);
    container.addEventListener('pointermove', move);
    container.addEventListener('pointerup', up);
    container.addEventListener('pointercancel', cancel);
    container.addEventListener('click', click, true);
    container.addEventListener('contextmenu', menu);
    container.addEventListener('dragstart', nativeDrag);
    window.addEventListener('keydown', key, true);
    return () => {
      finish(null, true);
      container.removeEventListener('pointerdown', down);
      container.removeEventListener('pointermove', move);
      container.removeEventListener('pointerup', up);
      container.removeEventListener('pointercancel', cancel);
      container.removeEventListener('click', click, true);
      container.removeEventListener('contextmenu', menu);
      container.removeEventListener('dragstart', nativeDrag);
      window.removeEventListener('keydown', key, true);
    };
  }, [containerRef, enabled]);

  return { box, hits };
}
