import axios from 'axios';
import { reportUI } from './telemetry';

import { backendBase as BACKEND_ORIGIN, proxyHeaders } from './runtime';
const API_BASE = `${BACKEND_ORIGIN}/api`;

export const backendAssetUrl = (path) => `${BACKEND_ORIGIN}${path}`;

const client = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
});

client.interceptors.request.use(config => {
  Object.assign(config.headers, proxyHeaders());
  return config;
});
client.interceptors.response.use(response => {
  if (String(response.headers['content-type'] || '').includes('text/html')) {
    throw new Error('The server returned a web page instead of API data. Your Jupyter session may have expired; reopen the app through Jupyter and retry.');
  }
  return response;
}, error => {
  if (!error.response && error.code !== 'ERR_CANCELED') reportUI('error', 'Backend request failed before a response: ' + (error.code || 'network error'));
  return Promise.reject(error);
});

export const api = {
  diagnostics: { list: params => client.get('/diagnostics', { params }) },
  groups: {
    list: () => client.get('/groups'),
    create: (data) => client.post('/groups', data),
    preview: (id, text, type = 'artist', additional_sources = []) => client.post(`/groups/${id}/collections/preview`, { text, type, additional_sources }),
    import: (id, text, type = 'artist', additional_sources = []) => client.post(`/groups/${id}/collections`, { text, type, additional_sources }),
    move: (folderId, groupId) => client.post(`/groups/folders/${folderId}/move`, { group_id: groupId }),
    sync: (id, options) => client.post(`/sync/group/${id}`, null, { params: options }),
    enableSource: (id, folderId) => client.post(`/groups/${id}/folders/${folderId}/enable-source`),
    history: (id) => client.get('/sync/jobs/history', { params: { group_id: id, limit: 20 } }),
    delete: (id) => client.delete(`/groups/${id}`),
    setProvider: (id, provider) => client.patch(`/groups/${id}/provider`, { provider }),
    blocked: (id) => client.get(`/groups/${id}/blocked`),
    block: (id, data) => client.post(`/groups/${id}/blocked`, data),
    unblock: (id, folderId) => client.delete(`/groups/${id}/blocked/${folderId}`),
  },
  dataset: {
    query: (id, data) => client.post(`/folders/${id}/images/query`, data),
    tags: (id, data, search = '', category = '') => client.post(`/folders/${id}/tags/query`, data, { params: { search, category: category || undefined } }),
    scan: (id, filters) => client.post(`/dataset/folders/${id}/scan`, filters),
    history: (id) => client.get('/dataset/jobs', { params: { folder_id: id } }),
    job: (id) => client.get(`/dataset/jobs/${id}`),
    issues: (id, offset = 0) => client.get(`/dataset/jobs/${id}/issues`, { params: { offset, limit: 100 } }),
    cancel: (id) => client.post(`/dataset/jobs/${id}/cancel`),
    resume: (id) => client.post(`/dataset/jobs/${id}/resume`),
    repair: (id, scan_id, issue_ids) => client.post(`/dataset/folders/${id}/repair`, { scan_id, issue_ids, confirmed: true }),
    preview: (id, data) => client.post(`/dataset/folders/${id}/preview`, data),
    review: (id, preview_token, image_ids, status) => client.post(`/dataset/folders/${id}/review`, { preview_token, image_ids, status, confirmed: true }),
    undo: (id, token) => client.post(`/dataset/folders/${id}/review/undo/${token}`),
  },
  // Folders
  folders: {
    list: () => client.get('/folders'),
    get: (id) => client.get(`/folders/${id}`),
    create: (data) => client.post('/folders', data),
    update: (id, data) => client.patch(`/folders/${id}`, data),
    updateSource: (id, provider, data) => client.patch(`/folders/${id}/sources/${provider}`, data),
    delete: (id) => client.delete(`/folders/${id}`),
    getImages: (id, limit = 100, offset = 0) =>
      client.get(`/folders/${id}/images`, { params: { limit, offset } }),
    bulkRemoveImages: (id, imageIds) => client.post(`/folders/${id}/images/bulk`, { image_ids: imageIds, action: 'remove' }),
    undoBulkRemove: (id, token) => client.post(`/folders/${id}/images/bulk/undo/${token}`),
    latestBulkRecovery: (id) => client.get(`/folders/${id}/images/bulk/recovery/latest`),
    reconcile: () => client.post('/folders/reconcile'),
  },

  // Images
  images: {
    get: (id) => client.get(`/images/${id}`),
    review: (id, data) => client.patch(`/images/${id}/review`, data),
    delete: (id) => client.delete(`/images/${id}`),
    decoderStatus: () => client.get('/images/media/decoder-status'),
  },

  duplicates: {
    list: (folderId, status = 'pending', limit = 100, offset = 0) =>
      client.get('/duplicates', { params: { folder_id: folderId, status, limit, offset } }),
    scan: (folderId, profile = 'balanced') =>
      client.post('/duplicates/scan', { folder_id: Number(folderId), profile }),
    job: (jobId) => client.get(`/duplicates/jobs/${jobId}`),
    resolve: (candidateId, action) =>
      client.post(`/duplicates/${candidateId}/resolve`, { action }),
  },

  // Tags
  tags: {
    search: (query, limit = 50) =>
      client.get('/tags/search', { params: { query, limit } }),
    getCollectionTags: (collectionId) =>
      client.get(`/tags/folder/${collectionId}`),
    getGroundTruth: (imageId) => client.get(`/tags/image/${imageId}/ground-truth`),
    replaceGroundTruth: (imageId, tags) => client.put(`/tags/image/${imageId}/ground-truth`, { tags }),
    bulkEdit: (collectionId, imageIds, tags, action) =>
      client.post(`/tags/folder/${collectionId}/bulk`, { image_ids: imageIds, tags, action }),
    bulkReplace: (collectionId, imageIds, oldTag, newTag) =>
      client.post(`/tags/folder/${collectionId}/bulk-replace`, { image_ids: imageIds, old_tag: oldTag, new_tag: newTag }),
    getGlobalCategoryPolicy: () => client.get('/tags/category-policy'),
    updateGlobalCategoryPolicy: (categories) => client.put('/tags/category-policy', { categories }),
    getFolderCategoryPolicy: (folderId) => client.get(`/tags/folder/${folderId}/category-policy`),
    updateFolderCategoryPolicy: (folderId, categories) => client.put(`/tags/folder/${folderId}/category-policy`, { categories }),
    undo: (token) => client.post(`/tags/undo/${token}`),
  },

  // Remote search
  search: (provider, query, cursor = null, limit = 50) =>
    client.get(`/search/${provider}`, { params: { query, cursor, limit } }),

  // Sync
  sync: {
    folder: (folderId, provider, limit = 20, options = {}) =>
      client.post(`/sync/folder/${folderId}/${provider}`, null, { params: { limit, ...options } }),
    all: ({ limit = 20, sort = 'latest', date_from, date_to } = {}) =>
      client.post('/sync/all', null, { params: { limit, sort, date_from, date_to } }),
    job: (jobId) => client.get(`/sync/jobs/${jobId}`),
    cancel: (jobId) => client.post(`/sync/jobs/${jobId}/cancel`),
    history: (limit = 50) => client.get('/sync/jobs/history', { params: { limit } }),
    getSchedule: () => client.get('/sync/schedule'),
    updateSchedule: (data) => client.put('/sync/schedule', data),
    runScheduleNow: () => client.post('/sync/schedule/run-now'),
  },

  // Health
  health: () => client.get('/health'),
  providers: {
    list: () => client.get('/providers'),
    status: (provider) => client.get(`/providers/${provider}/status`),
    gelbooruConfig: () => client.get('/providers/gelbooru/config'),
    saveGelbooruConfig: (data) => client.put('/providers/gelbooru/config', data),
    account: (site) => client.get(`/providers/${site}/account`),
    saveAccount: (site, data) => client.put(`/providers/${site}/account`, data),
    removeAccount: (site) => client.delete(`/providers/${site}/account`),
    pacing: () => client.get('/providers/pacing'),
    savePacing: (data) => client.put('/providers/pacing', data),
    galleryDlConfig: () => client.get('/providers/gallery-dl/config'),
    saveDeviantArtMedia: (mode) => client.put('/providers/deviantart/media', { mode }),
    saveDeviantArtConfig: (data) => client.put('/providers/deviantart/config', data),
    savePixivConfig: (data) => client.put('/providers/pixiv/config', data),
    saveTwitterConfig: (data) => client.put('/providers/twitter/config', data),
    uploadDeviantArtCookies: (file) => {
      const form = new FormData(); form.append('file', file);
      return client.post('/providers/deviantart/cookies', form, { headers: { 'Content-Type': 'multipart/form-data' } });
    },
    removeDeviantArtCookies: () => client.delete('/providers/deviantart/cookies'),
    uploadTwitterCookies: (file) => {
      const form = new FormData();
      form.append('file', file);
      return client.post('/providers/twitter/cookies', form, { headers: { 'Content-Type': 'multipart/form-data' } });
    },
    removeTwitterCookies: () => client.delete('/providers/twitter/cookies'),
  },
  settings: {
    getServer: () => client.get('/settings/server'),
    shutdown: () => client.post('/settings/shutdown', { confirm: true }),
    updateStorage: data => client.put('/settings/storage', data),
    getProcessing: () => client.get('/settings/processing'),
    updateProcessing: data => client.put('/settings/processing', data),
    getParallelism: () => client.get('/settings/parallelism'),
    updateParallelism: (workers) => client.put('/settings/parallelism', { workers }),
  },
  imports: {
    preview: (data, signal) => client.post('/imports/preview', data, { signal }),
    createBatch: (data) => client.post('/imports/batches', data),
    getBatch: (batchId) => client.get(`/imports/batches/${batchId}`),
    startBatch: (batchId) => client.post(`/imports/batches/${batchId}/start`),
    cancelBatch: (batchId) => client.post(`/imports/batches/${batchId}/cancel`),
    job: (jobId) => client.get(`/imports/jobs/${jobId}`),
  },
  planner: {
    status: () => client.get('/planner/status'),
    importArtists: (csv) => client.post('/planner/artists/import', { csv }),
    importCharacters: (csv) => client.post('/planner/characters/import', { csv }),
    fetchCharacters: (counts) => client.post('/planner/characters/fetch', counts),
    checkTags: (family, tags) => client.post('/planner/tags/check', { family, tags }),
    prioritySeries: (data) => client.post('/planner/characters/series', data),
    seriesStatus: () => client.get('/planner/characters/series'),
    clearPriority: () => client.delete('/planner/characters/priority'),
    deliver: (runId, groupPrefix) => client.post(`/planner/runs/${runId}/deliver`, { group_prefix: groupPrefix }),
    delivery: (id) => client.get(`/planner/deliveries/${id}`),
    resumeDelivery: (id) => client.post(`/planner/deliveries/${id}/resume`),
    deliveryProblems: (id) => client.get(`/planner/deliveries/${id}/problems`),
    cancelDelivery: () => client.post('/planner/deliveries/cancel'),
    trainingLayout: (id) => client.post(`/planner/deliveries/${id}/layout`),
    prunePreview: (id) => client.get(`/planner/deliveries/${id}/prune`),
    pruneApply: (id) => client.post(`/planner/deliveries/${id}/prune`, { confirmed: true }),
    enableOnly: (lines) => client.post('/planner/artists/enable-only', { lines }),
    enableAll: () => client.post('/planner/artists/enable-all'),
    artists: (params) => client.get('/planner/artists', { params }),
    artist: (id, runId) => client.get(`/planner/artists/${id}`, { params: { run_id: runId || undefined } }),
    override: (data) => client.post('/planner/overrides', data),
    harvest: (data) => client.post('/planner/harvest', data),
    cancelHarvest: () => client.post('/planner/harvest/cancel'),
    defaults: () => client.get('/planner/config/defaults'),
    run: (config) => client.post('/planner/runs', config),
    getRun: (id) => client.get(`/planner/runs/${id}`),
    exportRun: (id) => client.post(`/planner/runs/${id}/export`),
    folder: (folderId) => client.get(`/planner/folders/${folderId}`),
    candidates: (folderId, params) => client.get(`/planner/folders/${folderId}/candidates`, { params }),
    acceptCandidates: (folderId, posts) => client.post(`/planner/folders/${folderId}/accept`, { posts }),
    completeFolder: (folderId, complete) => client.post(`/planner/folders/${folderId}/complete`, { complete }),
    setMarks: (folderId, imageId, marks) => client.put(`/planner/folders/${folderId}/images/${imageId}/marks`, marks),
    markViewed: (folderId, imageId) => client.post(`/planner/folders/${folderId}/images/${imageId}/viewed`),
  },
  exports: {
    validate: (folderId) => client.get(`/exports/validate/${folderId}`),
    list: (folderId) => client.get('/exports', { params: { folder_id: folderId } }),
    create: (folderId, mode = 'copy') => client.post('/exports', { folder_id: Number(folderId), mode }),
    get: (exportId) => client.get(`/exports/${exportId}`),
  },
};

export default api;
