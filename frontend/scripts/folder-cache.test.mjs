import test from 'node:test';
import assert from 'node:assert/strict';
import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { refreshFolderViews } from '../src/api/folderCache.js';

test('delete, rescrape and undo refresh active sidebar, dashboard, detail and filtered pages together', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } });
  let serverCount = 10;
  const keys = [['collections'], ['collection', '7'], ['collection-images', '7', { rating: 'safe' }, 0, 'newest'], ['tag-explorer', 7, {}]];
  const observers = keys.map(queryKey => new QueryObserver(client, { queryKey, queryFn: async () => serverCount }));
  const unsubscribe = observers.map(o => o.subscribe(() => {}));
  try {
    await Promise.all(observers.map(o => o.refetch()));
    // The count must reflect accepted stored images, not a requested-post limit.
    for (const count of [0, 20, 0, 20, 23]) {
      serverCount = count;
      await refreshFolderViews(client, 7);
      for (const key of keys) assert.equal(client.getQueryData(key), count);
    }
    client.setQueryData(['collection', '8'], 50);
    await refreshFolderViews(client, '7');
    assert.equal(client.getQueryState(['collection', '8']).isInvalidated, false);
  } finally { unsubscribe.forEach(fn => fn()); client.clear(); }
});

test('Sync All and off-page import completion invalidate every folder cache, including inactive pages', async () => {
  const client = new QueryClient();
  try {
    const keys = [['collections'], ['collection', '1'], ['collection', '2'], ['collection-images', '2', {}, 100], ['tag-explorer', 2]];
    keys.forEach(key => client.setQueryData(key, 0));
    await refreshFolderViews(client);
    keys.forEach(key => assert.equal(client.getQueryState(key).isInvalidated, true));
  } finally { client.clear(); }
});
