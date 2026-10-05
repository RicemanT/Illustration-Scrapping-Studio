// Hand-assigned quality and aesthetic marks. Keys follow the keyboard layout:
// Q W E pick the quality mark, A S the aesthetic mark, D resets to normal.
export const MARKS = [
  { key: 'q', axis: 'quality', tag: 'masterpiece', label: 'Masterpiece', dot: 'bg-amber-400', badge: 'text-amber-200',
    active: 'border-amber-400/80 bg-amber-500/20 text-amber-50 shadow-[0_0_14px_-4px_rgba(251,191,36,0.7)]', hover: 'hover:border-amber-500/60' },
  { key: 'w', axis: 'quality', tag: 'best quality', label: 'Best quality', dot: 'bg-sky-400', badge: 'text-sky-200',
    active: 'border-sky-400/80 bg-sky-500/20 text-sky-50 shadow-[0_0_14px_-4px_rgba(56,189,248,0.7)]', hover: 'hover:border-sky-500/60' },
  { key: 'e', axis: 'quality', tag: 'low quality', label: 'Low quality', dot: 'bg-rose-400', badge: 'text-rose-200',
    active: 'border-rose-400/80 bg-rose-500/15 text-rose-50 shadow-[0_0_14px_-4px_rgba(251,113,133,0.7)]', hover: 'hover:border-rose-500/60' },
  { key: 'a', axis: 'aesthetic', tag: 'very aesthetic', label: 'Very aesthetic', dot: 'bg-fuchsia-400', badge: 'text-fuchsia-200',
    active: 'border-fuchsia-400/80 bg-fuchsia-500/20 text-fuchsia-50 shadow-[0_0_14px_-4px_rgba(232,121,249,0.7)]', hover: 'hover:border-fuchsia-500/60' },
  { key: 's', axis: 'aesthetic', tag: 'aesthetic', label: 'Aesthetic', dot: 'bg-teal-400', badge: 'text-teal-200',
    active: 'border-teal-400/80 bg-teal-500/20 text-teal-50 shadow-[0_0_14px_-4px_rgba(45,212,191,0.7)]', hover: 'hover:border-teal-500/60' },
  { key: 'd', axis: null, tag: null, label: 'Normal', dot: 'bg-slate-400', badge: 'text-slate-200',
    active: 'border-slate-400/80 bg-slate-500/20 text-slate-50 shadow-[0_0_14px_-4px_rgba(148,163,184,0.6)]', hover: 'hover:border-slate-500/60' },
];

export const markFor = (tag) => MARKS.find((mark) => mark.tag === tag);

export const marksOf = (image) => ({ quality: image?.quality_mark || null, aesthetic: image?.aesthetic_mark || null });

export const isActive = (mark, marks) => (mark.axis ? marks[mark.axis] === mark.tag : !marks.quality && !marks.aesthetic);

// Pressing a lit mark clears it; Normal clears both.
export const applyMark = (mark, marks) => (mark.axis
  ? { ...marks, [mark.axis]: marks[mark.axis] === mark.tag ? null : mark.tag }
  : { quality: null, aesthetic: null });

export const markTags = (marks) => [marks.quality, marks.aesthetic].filter(Boolean);
