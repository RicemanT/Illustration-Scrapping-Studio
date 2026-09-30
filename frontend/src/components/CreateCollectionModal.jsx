import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import api from '../api/client';

function CreateCollectionModal({ onClose }) {
  const queryClient = useQueryClient();
  const [formData, setFormData] = useState({
    name: '',
    group_id: null,
    type: 'artist',
    query: '',
    sources: ['danbooru'],
    source_queries: {},
    artist_tag_template: 'Drawn by {artist}',
  });
  const [formatArtistTags, setFormatArtistTags] = useState(true);
  const { data: providerData } = useQuery({
    queryKey: ['providers'],
    queryFn: async () => (await api.providers.list()).data,
  });
  const groups = useQuery({ queryKey: ['groups'], queryFn: async () => (await api.groups.list()).data });
  const providers = providerData?.items || [
    { name: 'danbooru', available: true }, { name: 'gelbooru', available: true },
    { name: 'e621', available: true }, { name: 'deviantart', available: true, collection_types: ['artist'] },
  ];

  const createMutation = useMutation({
    mutationFn: (data) => api.folders.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['collections'] });
      onClose();
    },
  });

  const handleSubmit = (e) => {
    e.preventDefault();
    createMutation.mutate({
      ...formData,
      source_queries: Object.fromEntries(Object.entries(formData.source_queries).filter(([source]) => formData.sources.includes(source))),
      artist_tag_template: formData.type === 'artist' && formatArtistTags ? formData.artist_tag_template.trim() : null,
    });
  };

  const handleSourceToggle = (source) => {
    setFormData((prev) => ({
      ...prev,
      sources: prev.sources.includes(source)
        ? prev.sources.filter((s) => s !== source)
        : [...prev.sources, source],
    }));
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-[#0c1219] rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b border-[#202a34]">
          <h2 className="text-2xl font-bold text-slate-100">Create Collection</h2>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-6">
          <label className="block text-slate-300">Group
            <select className="block w-full border rounded p-2" value={formData.group_id || ''} onChange={(event) => {
              const group = groups.data?.find((item) => item.id === Number(event.target.value));
              setFormData({ ...formData, group_id: group?.id || null, sources: group && providers.some(item => item.name === group.provider && (item.collection_types || ['artist','character','tag']).includes(formData.type)) ? [...new Set([...formData.sources, group.provider])] : formData.sources });
            }}><option value="">Ungrouped</option>{groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select>
          </label>
          <label className="block text-sm">Collection type
            <select aria-label="Collection type" className="block w-full rounded p-2" value={formData.type} onChange={e => {
              const kind = e.target.value;
              const sources = formData.sources.filter(name => (providers.find(p => p.name === name)?.collection_types || ['artist', 'character', 'tag']).includes(kind));
              setFormData({ ...formData, type: kind, sources, source_queries: Object.fromEntries(Object.entries(formData.source_queries).filter(([name]) => sources.includes(name))) });
            }}><option value="artist">Artist</option><option value="character">Character</option><option value="tag">Tag / query</option></select>
          </label>
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">
              Display name *
            </label>
            <input
              type="text"
              required
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              className="w-full px-3 py-2 border border-[#303d4c] rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="e.g., Quasarcake"
            />
          </div>

          {/* Artist tag formatting */}
          {formData.type === 'artist' && <div>
            <label className="flex items-center text-sm font-medium text-slate-300 mb-2">
              <input
                type="checkbox"
                checked={formatArtistTags}
                onChange={(e) => setFormatArtistTags(e.target.checked)}
                className="mr-2 h-4 w-4 text-blue-600 focus:ring-blue-500 border-[#303d4c] rounded"
              />
              Use collection name as artist caption trigger
            </label>
            <input
              type="text"
              required={formatArtistTags}
              disabled={!formatArtistTags}
              value={formData.artist_tag_template}
              onChange={(e) => setFormData({ ...formData, artist_tag_template: e.target.value })}
              className="w-full px-3 py-2 border border-[#303d4c] rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-[#1c2530] disabled:text-slate-400"
              placeholder="Drawn by {artist}"
            />
            <p className="mt-1 text-sm text-slate-400">
              Use {'{artist}'} as the name placeholder. The collection name becomes one artist trigger. Source credits remain preserved as provenance.
            </p>
          </div>}

          {/* Query */}
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">
              Query *
            </label>
            <input
              type="text"
              required
              value={formData.query}
              onChange={(e) => setFormData({ ...formData, query: e.target.value })}
              className="w-full px-3 py-2 border border-[#303d4c] rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder={formData.type === 'tag' ? 'landscape sunset -monochrome' : formData.type === 'character' ? 'hatsune miku' : 'namako daibakuhatsu'}
            />
            <p className="mt-1 text-sm text-slate-400">
              {formData.type === 'tag' ? 'Use provider tag syntax: spaces separate tags; underscores belong within one tag. Filters and exclusions are preserved.' : 'Enter one name; spaces and underscores refer to the same identity.'} Search terms and display names are not automatically added to image captions.
            </p>
          </div>

          {/* Sources */}
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-2">
              Sources (select any number)
            </label>
            <div className="space-y-2">
              {providers.map((provider) => (
                <label key={provider.name} className={`flex items-start ${provider.available ? '' : 'opacity-50'}`}>
                  <input
                    type="checkbox"
                    checked={formData.sources.includes(provider.name)}
                    onChange={() => handleSourceToggle(provider.name)}
                    disabled={!(provider.collection_types || ['artist', 'character', 'tag']).includes(formData.type)}
                    className="mr-2 h-4 w-4 text-blue-600 focus:ring-blue-500 border-[#303d4c] rounded"
                  />
                  <span className="text-slate-300">
                    {!(provider.collection_types || ['artist', 'character', 'tag']).includes(formData.type) && <span className="block text-xs text-amber-300">Artist profiles only</span>}
                    <span className="capitalize">{provider.name === 'twitter' ? 'Twitter / X' : provider.name}</span>
                    {!provider.available && provider.unavailable_reason && <span className="ml-2 text-xs text-amber-600">{provider.unavailable_reason}</span>}
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div className="space-y-2"><p className="text-sm text-slate-400">Optional provider-specific searches override the default query.</p>{formData.sources.map(source => <label key={source} className="block text-xs">{source}<input aria-label={`${source} search override`} className="block w-full rounded p-2" value={formData.source_queries[source] || ''} placeholder="Use default query" onChange={e => setFormData({ ...formData, source_queries: { ...formData.source_queries, [source]: e.target.value } })} /></label>)}</div>
          {/* Actions */}
          <div className="flex justify-end space-x-3 pt-4 border-t border-[#202a34]">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 border border-[#303d4c] rounded-lg hover:bg-[#11161d]"
              disabled={createMutation.isPending}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || (formData.type === 'artist' && formatArtistTags && !formData.artist_tag_template.includes('{artist}'))}
              className="px-4 py-2 bg-[#344a73] text-white rounded-lg hover:bg-[#405b88] disabled:opacity-50"
            >
              {createMutation.isPending ? 'Creating...' : 'Create Collection'}
            </button>
          </div>

          {createMutation.isError && (
            <div className="mt-4 p-3 bg-red-950/50 border border-red-900 rounded-lg text-red-300">
              Error: {(typeof createMutation.error.response?.data?.detail === 'string' ? createMutation.error.response.data.detail : createMutation.error.message)}
            </div>
          )}
        </form>
      </div>
    </div>
  );
}

export default CreateCollectionModal;
