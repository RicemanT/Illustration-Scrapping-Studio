import React, { useState } from 'react';

function SyncAllModal({ onClose, onStart, pending, error, group }) {
  const [limit, setLimit] = useState(20);
  const [sort, setSort] = useState('latest');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const invalidRange = Boolean(dateFrom && dateTo && dateFrom > dateTo);

  const submit = (event) => {
    event.preventDefault();
    if (invalidRange) return;
    onStart({
      limit: Math.min(Math.max(Number.parseInt(limit, 10) || 20, 1), 320),
      sort,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onMouseDown={(event) => event.target === event.currentTarget && !pending && onClose()}>
      <form onSubmit={submit} className="w-full max-w-lg rounded-lg border border-[#2b3746] bg-[#10161e] shadow-2xl">
        <div className="border-b border-[#26313e] px-5 py-4">
          <h2 className="text-lg font-semibold text-slate-100">{group ? `Scrape ${group.name}` : 'Sync All options'}</h2>
          <p className="mt-1 text-xs text-slate-400">{group ? `Only enabled collections in this group, using ${group.provider}.` : 'These options apply to every enabled folder/source in this run.'}</p>
        </div>

        <div className="space-y-4 p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs text-slate-400">
              Amount per folder/source
              <input type="number" min="1" max="320" required value={limit} onChange={(event) => setLimit(event.target.value)} className="mt-1 block w-full rounded border border-[#334155] bg-[#090d12] px-3 py-2 text-sm text-slate-100" />
            </label>
            <label className="text-xs text-slate-400">
              Order
              <select value={sort} onChange={(event) => setSort(event.target.value)} className="mt-1 block w-full rounded border border-[#334155] bg-[#090d12] px-3 py-2 text-sm text-slate-100">
                <option value="latest">Latest first</option>
                <option value="oldest">Oldest first</option>
              </select>
            </label>
            <label className="text-xs text-slate-400">
              From date (optional)
              <input type="date" value={dateFrom} max={dateTo || undefined} onChange={(event) => setDateFrom(event.target.value)} className="mt-1 block w-full rounded border border-[#334155] bg-[#090d12] px-3 py-2 text-sm text-slate-100" />
            </label>
            <label className="text-xs text-slate-400">
              To date (optional)
              <input type="date" value={dateTo} min={dateFrom || undefined} onChange={(event) => setDateTo(event.target.value)} className="mt-1 block w-full rounded border border-[#334155] bg-[#090d12] px-3 py-2 text-sm text-slate-100" />
            </label>
          </div>

          <p className="text-xs leading-5 text-slate-400">
            {group ? 'The amount applies separately to each collection through the group provider. ' : 'A folder with multiple enabled providers can receive up to this amount from each provider. '}Date-limited and oldest-first runs do not change the automatic latest/backfill cursors.
          </p>
          {invalidRange && <p className="text-xs text-red-400">The From date must be on or before the To date.</p>}
          {error && <p className="text-xs text-red-400">{error.response?.data?.detail || error.message}</p>}
        </div>

        <div className="flex justify-end gap-3 border-t border-[#26313e] px-5 py-4">
          <button type="button" onClick={onClose} disabled={pending} className="rounded border border-[#334155] px-4 py-2 text-sm text-slate-300 hover:bg-[#18212d] disabled:opacity-50">Cancel</button>
          <button type="submit" disabled={pending || invalidRange} className="rounded bg-[#344a73] px-4 py-2 text-sm font-medium text-white hover:bg-[#405b88] disabled:opacity-50">{pending ? 'Starting...' : group ? 'Start group scrape' : 'Start Sync All'}</button>
        </div>
      </form>
    </div>
  );
}

export default SyncAllModal;
