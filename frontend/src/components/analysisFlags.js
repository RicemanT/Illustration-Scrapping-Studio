// Review flags from the image analysis: short tile labels, full names and how serious they are.
export const FLAGS = {
  off_style: { short: 'off-style', label: 'Off-style', level: 3 },
  content_rough: { short: 'sketch', label: 'Sketch or rough', level: 3 },
  content_monochrome: { short: 'mono', label: 'Monochrome', level: 3 },
  content_comic: { short: 'comic', label: 'Comic page', level: 3 },
  content_3d: { short: '3D', label: '3D render', level: 3 },
  content_photo: { short: 'photo', label: 'Photo', level: 3 },
  content_ai: { short: 'AI', label: 'AI-generated', level: 3 },
  low_aesthetic: { short: 'weak', label: "Among the dataset's weakest", level: 3 },
  near_duplicate: { short: 'dup', label: 'Near-duplicate of another pick', level: 2 },
  ai_suspect: { short: 'AI?', label: 'Possibly AI-generated', level: 2 },
  style_borderline: { short: 'style?', label: 'Borderline style', level: 1 },
  low_aesthetic_here: { short: 'weakest', label: "Among this artist's weakest", level: 1 },
  not_analyzed: { short: 'n/a', label: 'Not analysed', level: 1 },
};

export const LEVEL_CLASS = {
  3: 'bg-red-950/90 text-red-100 ring-1 ring-red-700/70',
  2: 'bg-amber-950/90 text-amber-100 ring-1 ring-amber-700/60',
  1: 'bg-black/80 text-slate-200',
};

export function parseFlags(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  try { return JSON.parse(value) || []; } catch { return []; }
}

export const flagInfo = (flag) => FLAGS[flag] || { short: flag.replaceAll('_', ' '), label: flag.replaceAll('_', ' '), level: 1 };
