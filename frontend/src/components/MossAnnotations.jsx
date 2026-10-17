import React from 'react';

// MOSS-Image annotations from the captioning script (moss-caption.py): <image>_moss.json (captions at three
// lengths + watermark boxes), <image>_layout.json (summary + character / object / text items) and the cached
// OCR lines. Boxes are [y_min, x_min, y_max, x_max] normalized to 0-1000 of the full image. The overlay draws
// them in the image's own pixel space, centred and contained like the <img> (object-contain), so they stay on
// the picture however the viewer letterboxes it.

export const LABEL_STYLE = {
  character: { stroke: '#38bdf8', text: 'text-sky-300' },
  object: { stroke: '#fbbf24', text: 'text-amber-300' },
  text: { stroke: '#34d399', text: 'text-emerald-300' },
  watermark: { stroke: '#f87171', text: 'text-red-300' },
};

export function boxesOf(annotations) {
  const boxes = [];
  (annotations?.layout?.items || []).forEach((item, index) => {
    if (Array.isArray(item.box_2d)) boxes.push({ key: `item-${index}`, number: index + 1, label: item.label, box: item.box_2d });
  });
  (annotations?.moss?.watermark || []).forEach((mark, index) => {
    if (Array.isArray(mark.box_2d)) boxes.push({ key: `wm-${index}`, number: `W${index + 1}`, label: 'watermark', box: mark.box_2d });
  });
  return boxes;
}

// Absolutely positioned over the image element; the parent must wrap the image tightly (relative, inline-block).
export function BoxOverlay({ boxes, width, height, highlight, onHover }) {
  if (!boxes.length || !width || !height) return null;
  const sx = width / 1000, sy = height / 1000, font = Math.max(width, height) / 55;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet" data-overlay="moss-boxes"
      className="pointer-events-none absolute inset-0 h-full w-full">
      {boxes.map(({ key, number, label, box }) => {
        const [y0, x0, y1, x1] = [box[0] * sy, box[1] * sx, box[2] * sy, box[3] * sx];
        const style = LABEL_STYLE[label] || LABEL_STYLE.object;
        const active = highlight === key;
        return (
          <g key={key} className="pointer-events-auto" onMouseEnter={() => onHover?.(key)} onMouseLeave={() => onHover?.(null)}>
            <rect x={x0} y={y0} width={x1 - x0} height={y1 - y0} fill={active ? `${style.stroke}33` : 'none'}
              stroke={style.stroke} strokeWidth={active ? 4 : 2} vectorEffect="non-scaling-stroke" />
            <text x={x0 + font * 0.25} y={y0 + font * 1.1} fill={style.stroke} fontSize={font} fontWeight="bold" style={{ paintOrder: 'stroke' }}
              stroke="#000" strokeWidth="3" vectorEffect="non-scaling-stroke">{number}</text>
          </g>
        );
      })}
    </svg>
  );
}

function Caption({ title, text }) {
  if (!text) return null;
  return (
    <details className="rounded border border-slate-800 p-2" open={title === 'Short'}>
      <summary className="cursor-pointer text-xs font-semibold text-slate-300">{title}</summary>
      <p className="mt-1 whitespace-pre-wrap text-xs text-slate-300">{text}</p>
    </details>
  );
}

export function AnnotationPanel({ annotations, highlight, onHover }) {
  const moss = annotations?.moss;
  const layout = annotations?.layout;
  if (!moss && !layout) return null;
  return (
    <div className="space-y-3">
      {moss && <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-100">MOSS captions</h3>
        {moss.error ? <p className="text-xs text-red-300">{moss.error}</p> : <div className="space-y-1.5">
          <Caption title="Short" text={moss.caption?.short} />
          <Caption title="Medium" text={moss.caption?.medium} />
          <Caption title="Long" text={moss.caption?.long} />
          <p className="text-[11px] text-slate-500">
            {moss.watermark == null ? 'Watermarks not checked yet (converted caption)' : `${moss.watermark.length} watermark(s)`}
            {Array.isArray(moss.borders) && moss.borders[0] !== 'none' ? ` · borders: ${moss.borders.join(', ')}` : ''}
            {moss.source ? ` · ${moss.source.provider} / ${moss.source.model}` : ''}
          </p>
        </div>}
      </div>}
      {layout && <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-100">Layout</h3>
        {layout.error ? <p className="text-xs text-red-300">{layout.error}</p> : <>
          <p className="mb-2 text-xs text-slate-400">
            {(layout.summary?.meta || []).join(', ') || 'no meta'} · {layout.summary?.canvas || 'single'}
          </p>
          <ol className="space-y-1">
            {(layout.items || []).map((item, index) => {
              const key = `item-${index}`;
              const style = LABEL_STYLE[item.label] || LABEL_STYLE.object;
              return (
                <li key={key} onMouseEnter={() => onHover?.(key)} onMouseLeave={() => onHover?.(null)}
                  className={`rounded px-1.5 py-1 text-xs ${highlight === key ? 'bg-slate-800' : ''}`}>
                  <span className={`font-semibold ${style.text}`}>{index + 1}. {item.label}</span>
                  {item.tags?.length ? <span className="text-slate-400"> [{item.tags.join(', ')}]</span> : null}
                  {item.text ? <span className="text-emerald-200"> “{item.text.replaceAll('\n', ' / ')}”{item.from_ocr ? ' (OCR)' : ''}</span> : null}
                  <div className="text-slate-400">{item.caption}</div>
                </li>
              );
            })}
          </ol>
        </>}
      </div>}
    </div>
  );
}
