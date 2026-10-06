import React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

// Posted-year histogram for a planner collection. Clicking a year keeps images
// posted from that year on: older ones are dimmed, can be selected in one go,
// and the wildcards default to the era.
export default function EraBar({ folderId, context, onSelectOlder, disabled }) {
  const queryClient = useQueryClient();
  const setEra = useMutation({
    mutationFn: (year) => api.planner.setEra(folderId, year),
    onSuccess: ({ data }) => {
      queryClient.setQueryData(['planner-folder', String(folderId)], data);
      queryClient.invalidateQueries({ queryKey: ['planner-candidates', folderId] });
    },
  });
  const years = Object.entries(context.years || {}).map(([year, count]) => [Number(year), count]);
  if (!years.length) return null;
  const most = Math.max(...years.map(([, count]) => count));
  const era = context.era_from;
  const older = context.older_ids || [];
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-[#202a34] px-3 py-2 text-xs">
      <span className="text-slate-400" title="When each image was originally posted. Click a year to keep images from that year on.">Posted</span>
      <div className="flex items-end gap-0.5" role="group" aria-label="Images per posted year">
        {years.map(([year, count]) => {
          const before = Boolean(era && year < era);
          const chosen = era === year;
          return (
            <button key={year} type="button" disabled={disabled || setEra.isPending}
              onClick={() => setEra.mutate(chosen ? null : year)}
              title={`${count} ${count === 1 ? 'image' : 'images'} posted in ${year}. ${chosen ? 'Click to clear the era.' : `Click to keep ${year} onward.`}`}
              className={`flex flex-col items-center rounded px-1 pt-1 disabled:cursor-not-allowed ${chosen ? 'bg-amber-900/40 ring-1 ring-amber-600/70' : 'hover:bg-[#1b2539]'}`}>
              <span className={`w-6 rounded-sm ${before ? 'bg-slate-700' : 'bg-blue-500/70'}`} style={{ height: 4 + Math.round((18 * count) / most) }} />
              <span className={`mt-0.5 text-[10px] tabular-nums ${before ? 'text-slate-500 line-through' : 'text-slate-300'}`}>{year}</span>
              <span className="text-[9px] tabular-nums text-slate-500">{count}</span>
            </button>
          );
        })}
      </div>
      {context.undated > 0 && <span className="text-slate-500" title="No posting date in the source metadata">{context.undated} undated</span>}
      {era ? <>
        <span className="text-amber-200">Keeping {era} onward</span>
        {older.length > 0 && <button type="button" disabled={disabled} onClick={() => onSelectOlder(older)}
          className="rounded bg-amber-900/50 px-2 py-1 text-amber-100 hover:bg-amber-800/60 disabled:opacity-50">Select {older.length} older</button>}
        <button type="button" disabled={disabled || setEra.isPending} onClick={() => setEra.mutate(null)} className="text-slate-400 hover:text-slate-200">Clear era</button>
      </> : <span className="text-slate-500">Click a year to keep only images posted from then on.</span>}
      {setEra.isError && <span className="text-red-300">{setEra.error.response?.data?.detail || setEra.error.message}</span>}
    </div>
  );
}
