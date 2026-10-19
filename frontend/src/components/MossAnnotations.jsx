import React from 'react';

// MOSS-Image annotations from the captioning script (moss-caption.py): <image>_moss.json (captions at three
// lengths + watermark boxes), <image>_layout.json (summary + character / object / text items) and the cached
// detections (<image>_ocr.json: text regions or OCR lines, and with the GPU detectors faces, panels and
// watermarks). Boxes are [y_min, x_min, y_max, x_max] normalized to 0-1000 of the full image. The overlay draws
// them in the image's own pixel space, centred and contained like the <img> (object-contain), so they stay on
// the picture however the viewer letterboxes it.

export const LABEL_STYLE = {
  character: { stroke: '#38bdf8', text: 'text-sky-300' },
  object: { stroke: '#fbbf24', text: 'text-amber-300' },
  text: { stroke: '#34d399', text: 'text-emerald-300' },
  watermark: { stroke: '#f87171', text: 'text-red-300' },
  panel: { stroke: '#c084fc', text: 'text-purple-300' },   // MageTrail's comic / collage panel items
  ocr: { stroke: '#2dd4bf', text: 'text-teal-300', dash: '6 4' },
  face: { stroke: '#f472b6', text: 'text-pink-300', dash: '6 4' },
  panel: { stroke: '#a78bfa', text: 'text-violet-300', dash: '10 6' },
  detectedWatermark: { stroke: '#fb923c', text: 'text-orange-300', dash: '6 4' },
};

// Detector layers of <image>_ocr.json, in drawing order (panels underneath).
export const DETECTION_LAYERS = [
  { id: 'panels', label: 'Panels', style: 'panel', field: 'panels', prefix: 'P' },
  { id: 'text', label: 'Text', style: 'ocr', field: 'lines', prefix: 'T' },
  { id: 'faces', label: 'Faces', style: 'face', field: 'faces', prefix: 'F' },
  { id: 'watermarks', label: 'Watermarks', style: 'detectedWatermark', field: 'watermarks', prefix: 'W' },
];
export const DEFAULT_LAYERS = { panels: true, text: true, faces: true, watermarks: true };

// Numbers match what the layout model saw: text T0.. are its OCR line numbers, faces F0.. its face numbers.
export function detectionBoxesOf(annotations, layers = DEFAULT_LAYERS) {
  const ocr = annotations?.ocr;
  if (!ocr || ocr.error) return [];
  const boxes = [];
  DETECTION_LAYERS.forEach((layer) => {
    if (!layers[layer.id]) return;
    (ocr[layer.field] || []).forEach((entry, index) => {
      if (Array.isArray(entry.box_2d)) boxes.push({ key: `det-${layer.id}-${index}`, number: `${layer.prefix}${index}`, label: layer.style, box: entry.box_2d });
    });
  });
  return boxes;
}

export function detectionCount(annotations) {
  const ocr = annotations?.ocr;
  if (!ocr || ocr.error) return 0;
  return DETECTION_LAYERS.reduce((sum, layer) => sum + (ocr[layer.field]?.length || 0), 0);
}

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
              stroke={style.stroke} strokeWidth={active ? 4 : 2} strokeDasharray={active ? undefined : style.dash} vectorEffect="non-scaling-stroke" />
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

const percent = (score) => (typeof score === 'number' ? `${Math.round(score * 100)}%` : '');

function DetectionRow({ id, highlight, onHover, style, number, children }) {
  return (
    <li onMouseEnter={() => onHover?.(id)} onMouseLeave={() => onHover?.(null)}
      className={`rounded px-1.5 py-1 text-xs ${highlight === id ? 'bg-slate-800' : ''}`}>
      <span className={`font-semibold ${LABEL_STYLE[style].text}`}>{number}</span> {children}
    </li>
  );
}

// What the local detectors found (<image>_ocr.json), to judge them before the layout pass relies on them.
export function DetectionPanel({ annotations, layers, onLayers, visible, highlight, onHover }) {
  const ocr = annotations?.ocr;
  if (!ocr) return null;
  if (ocr.error) return <div><h3 className="mb-1 text-sm font-semibold text-slate-100">Detections</h3><p className="text-xs text-red-300">{ocr.error}</p></div>;
  const engine = ocr.detectors?.text || 'rapidocr';
  const lines = ocr.lines || [];
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-100">Detections</h3>
        <span className="text-[11px] text-slate-500">{({ anzhc: 'Anzhc text-seg', anzhc_vl: 'Anzhc text-seg + PaddleOCR-VL', rapidocr: 'RapidOCR' })[engine] || engine}{ocr.size ? ` · ${ocr.size[0]}×${ocr.size[1]}` : ''}</span>
      </div>
      <div className="mb-2 flex flex-wrap gap-1.5" role="group" aria-label="Detection layers">
        {DETECTION_LAYERS.filter((layer) => Array.isArray(ocr[layer.field])).map((layer) => {
          const on = Boolean(layers[layer.id]);
          return (
            <button key={layer.id} type="button" aria-pressed={on} onClick={() => onLayers({ ...layers, [layer.id]: !on })}
              className={`rounded border px-2 py-0.5 text-[11px] ${on ? `border-slate-500 ${LABEL_STYLE[layer.style].text}` : 'border-slate-700 text-slate-500'}`}>
              {layer.label} ({ocr[layer.field].length})
            </button>
          );
        })}
      </div>
      {!visible && <p className="mb-1 text-[11px] text-slate-500">Boxes hidden: use "Detections" above the image.</p>}
      {!lines.length && <p className="text-xs text-slate-500">No text found.</p>}
      <ol className="space-y-0.5">
        {lines.map((line, index) => (
          <DetectionRow key={index} id={`det-text-${index}`} number={`T${index}`} style="ocr" highlight={highlight} onHover={onHover}>
            {line.kind && <span className="text-slate-400">{line.kind} · </span>}
            <span className="text-slate-500">{percent(line.score)}</span>
            <div className="whitespace-pre-wrap text-emerald-200">{line.text}</div>
          </DetectionRow>
        ))}
        {(ocr.watermarks || []).map((mark, index) => (
          <DetectionRow key={`w${index}`} id={`det-watermarks-${index}`} number={`W${index}`} style="detectedWatermark" highlight={highlight} onHover={onHover}>
            <span className="text-slate-400">watermark · </span><span className="text-slate-500">{percent(mark.score)}</span>
            <div className="whitespace-pre-wrap text-orange-200">{mark.text || '(unread)'}</div>
          </DetectionRow>
        ))}
      </ol>
      {(ocr.faces?.length > 0 || ocr.panels?.length > 0) && (
        <div className="mt-1.5 text-[11px] text-slate-400">
          {ocr.faces?.length > 0 && <div>Faces: {ocr.faces.map((face, index) => (
            <span key={index} onMouseEnter={() => onHover?.(`det-faces-${index}`)} onMouseLeave={() => onHover?.(null)}
              className={`mr-1 cursor-default rounded px-1 ${highlight === `det-faces-${index}` ? 'bg-slate-800' : ''}`}>F{index} {percent(face.score)}</span>))}</div>}
          {ocr.panels?.length > 0 && <div>{ocr.panels.length} panels (canvas hint)</div>}
        </div>
      )}
    </div>
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
                  {item.short && <div className="text-slate-500"><span className="text-slate-600">short:</span> {item.short}</div>}
                </li>
              );
            })}
          </ol>
        </>}
      </div>}
    </div>
  );
}
