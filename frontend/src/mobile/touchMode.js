import { useEffect, useState } from 'react';

// Touch mode: the phone/tablet interface. Automatic on touch screens and narrow windows; the menu can force
// either view, remembered per browser.
const KEY = 'artist.viewMode';
const EVENT = 'artist-view-mode';
const QUERY = '(pointer: coarse), (max-width: 767px)';

const readOverride = () => {
  try { return localStorage.getItem(KEY); } catch { return null; }
};
const matchesTouch = () => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(QUERY).matches : false);

export function useTouchMode() {
  const [override, setOverride] = useState(readOverride);
  const [auto, setAuto] = useState(matchesTouch);
  useEffect(() => {
    if (!window.matchMedia) return undefined;
    const media = window.matchMedia(QUERY);
    const changed = () => setAuto(media.matches);
    media.addEventListener?.('change', changed);
    const synced = () => setOverride(readOverride());
    window.addEventListener(EVENT, synced);
    return () => { media.removeEventListener?.('change', changed); window.removeEventListener(EVENT, synced); };
  }, []);
  const setMode = (mode) => {
    try { if (mode) localStorage.setItem(KEY, mode); else localStorage.removeItem(KEY); } catch { /* storage unavailable */ }
    setOverride(mode);
    window.dispatchEvent(new Event(EVENT));
  };
  const touch = override === 'touch' ? true : override === 'desktop' ? false : auto;
  return { touch, mode: override || 'auto', setMode };
}
