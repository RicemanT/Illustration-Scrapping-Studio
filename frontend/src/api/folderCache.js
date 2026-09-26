// All views of folder membership must be invalidated together. Counts always
// come from the backend, never from requested posts or downloaded-file totals.
export const FOLDER_COUNT_REFRESH_MS = 5000;

export function refreshFolderViews(queryClient, folderId = null) {
  const keys = folderId == null
    ? [['collections'], ['collection'], ['collection-images'], ['tag-explorer'], ['collection-tags']]
    : [['collections'], ['collection', String(folderId)], ['collection-images', String(folderId)],
      ['tag-explorer', Number(folderId)], ['collection-tags', Number(folderId)]];
  return Promise.all(keys.map(queryKey => queryClient.invalidateQueries({ queryKey })));
}
