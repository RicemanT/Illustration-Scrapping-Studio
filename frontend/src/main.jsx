import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider, MutationCache } from '@tanstack/react-query';
import App from './App';
import { appBase } from './api/runtime';
import { ErrorBoundary, Notifications, notify } from './components/Feedback';
import './index.css';
import { installTelemetry } from './api/telemetry';
installTelemetry();

const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    onError: error => { if (error.code === 'ERR_CANCELED' || error.name === 'AbortError') return; const detail = error.response?.data?.detail; notify(typeof detail === 'string' ? detail : error.message || 'The operation failed. Please retry.', true, error.response?.data?.request_id || error.response?.headers?.['x-request-id']); },
    onSuccess: (_data, _variables, _context, mutation) => { if (mutation.options.meta?.successMessage) notify(mutation.options.meta.successMessage); },
  }),
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={appBase || "/"}>
        <ErrorBoundary><App /></ErrorBoundary>
        <Notifications />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
