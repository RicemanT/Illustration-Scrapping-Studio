import { useCallback, useEffect, useRef, useState } from 'react';

const LONG_PRESS_MS = 420;
const EDGE = 90;        // pixels from the top or bottom edge where sliding starts to scroll
const MAX_SPEED = 20;   // pixels per frame at the very edge

const scrollerOf = (node) => {
  for (let el = node?.parentElement; el && el !== document.body; el = el.parentElement) {
    const overflow = getComputedStyle(el).overflowY;
    if ((overflow === 'auto' || overflow === 'scroll') && el.scrollHeight > el.clientHeight) return el;
  }
  return document.scrollingElement || document.documentElement;
};

// Long-press a tile (an element with data-select-key) to start selecting; keep the finger down and slide to select
// every tile between the first one and the one under the finger, as phone galleries do (sliding back shrinks it).
// Near the top or bottom edge the page scrolls by itself. Shared by the image grid and the wildcards so both
// always behave the same.
export default function useTouchSelection(gridRef, deps = []) {
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const selectingRef = useRef(false);
  selectingRef.current = selecting;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const suppressClick = useRef(false);

  const toggle = useCallback((key, on) => setSelected((current) => {
    const next = new Set(current);
    if (on ?? !next.has(key)) next.add(key); else next.delete(key);
    return next;
  }), []);

  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return undefined;
    let press = null;
    let frame = null;
    const tileAt = (x, y) => document.elementFromPoint(x, y)?.closest?.('[data-select-key]');
    // Over a bar at the bottom (or top) of the screen there is no tile: use the nearest one inside the grid.
    const keyAt = (x, y) => {
      for (let step = 0; step <= EDGE; step += 15) {
        const tile = tileAt(x, y - step) || tileAt(x, y + step);
        if (tile && grid.contains(tile)) return tile.dataset.selectKey;
      }
      return null;
    };
    const extend = (key) => {
      if (!press?.active || !key || key === press.last) return;
      press.last = key;
      const keys = [...grid.querySelectorAll('[data-select-key]')].map((tile) => tile.dataset.selectKey);
      const from = keys.indexOf(press.anchor);
      const to = keys.indexOf(key);
      if (from < 0 || to < 0) return;
      const next = new Set(press.base);
      keys.slice(Math.min(from, to), Math.max(from, to) + 1).forEach((k) => next.add(k));
      setSelected(next);
    };
    const scroll = () => {
      if (!press?.active) { frame = null; return; }
      const box = press.scroller === document.scrollingElement || press.scroller === document.documentElement
        ? { top: 0, bottom: window.innerHeight } : press.scroller.getBoundingClientRect();
      let speed = 0;
      if (press.y > box.bottom - EDGE) speed = Math.min(1, (press.y - (box.bottom - EDGE)) / EDGE) * MAX_SPEED;
      else if (press.y < box.top + EDGE) speed = -Math.min(1, (box.top + EDGE - press.y) / EDGE) * MAX_SPEED;
      if (speed) {
        press.scroller.scrollTop += speed;
        extend(keyAt(press.x, press.y));
      }
      frame = requestAnimationFrame(scroll);
    };
    const start = (event) => {
      const tile = event.target.closest?.('[data-select-key]');
      if (!tile || event.touches.length > 1) return;
      const touch = event.touches[0];
      const key = tile.dataset.selectKey;
      press = { x: touch.clientX, y: touch.clientY, sx: touch.clientX, sy: touch.clientY, active: false, scroller: scrollerOf(grid) };
      press.timer = setTimeout(() => {
        if (!press) return;
        press.active = true;
        press.anchor = key;
        press.last = key;
        press.base = new Set(selectedRef.current);
        press.base.add(key);
        suppressClick.current = true;
        setSelecting(true);
        setSelected(new Set(press.base));
        navigator.vibrate?.(15);
        frame = requestAnimationFrame(scroll);
      }, LONG_PRESS_MS);
    };
    const move = (event) => {
      if (!press) return;
      const touch = event.touches[0];
      press.x = touch.clientX;
      press.y = touch.clientY;
      if (!press.active) {
        if (Math.hypot(touch.clientX - press.sx, touch.clientY - press.sy) > 10) { clearTimeout(press.timer); press = null; }
        return;
      }
      event.preventDefault(); // the finger is selecting, not scrolling
      extend(keyAt(touch.clientX, touch.clientY));
    };
    const end = () => {
      if (press) {
        clearTimeout(press.timer);
        // Some phones send a click after a long-press and some do not: ignore only one that comes right away.
        if (press.active) setTimeout(() => { suppressClick.current = false; }, 350);
      }
      if (frame) cancelAnimationFrame(frame);
      frame = null;
      press = null;
    };
    grid.addEventListener('touchstart', start, { passive: true });
    grid.addEventListener('touchmove', move, { passive: false });
    grid.addEventListener('touchend', end);
    grid.addEventListener('touchcancel', end);
    return () => {
      end();
      grid.removeEventListener('touchstart', start);
      grid.removeEventListener('touchmove', move);
      grid.removeEventListener('touchend', end);
      grid.removeEventListener('touchcancel', end);
    };
  }, deps);

  // A tap right after a long-press is part of it, not a separate tap.
  const consumeClick = () => {
    if (!suppressClick.current) return false;
    suppressClick.current = false;
    return true;
  };
  const clear = () => { setSelected(new Set()); setSelecting(false); };
  return { selecting, setSelecting, selected, setSelected, toggle, consumeClick, clear, selectingRef };
}
