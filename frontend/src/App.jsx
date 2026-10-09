import React from 'react';
import { Navigate, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './routes/Dashboard';
import CollectionView from './routes/CollectionView';
import Settings from './routes/Settings';
import Groups from './routes/Groups';
import Logs from './routes/Logs';
import Planner from './routes/Planner';
import Tracker from './routes/Tracker';
import TouchLayout from './mobile/TouchLayout';
import TouchFolder from './mobile/TouchFolder';
import ReviewQueue from './mobile/ReviewQueue';
import { useTouchMode } from './mobile/touchMode';

// Phones and tablets get the touch interface; the menu can switch either way.
function Shell() {
  const { touch } = useTouchMode();
  return touch ? <TouchLayout /> : <Layout />;
}

function Home() {
  const { touch } = useTouchMode();
  return touch ? <Navigate to="/review" replace /> : <Dashboard />;
}

function FolderPage() {
  const { touch } = useTouchMode();
  return touch ? <TouchFolder /> : <CollectionView />;
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<Shell />}>
        <Route index element={<Home />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="review" element={<ReviewQueue />} />
        <Route path="folder/:id" element={<FolderPage />} />
        <Route path="collection/:id" element={<FolderPage />} />
        <Route path="logs" element={<Logs />} />
        <Route path="settings" element={<Settings />} />
        <Route path="groups" element={<Groups />} />
        <Route path="groups/:groupId" element={<Groups />} />
        <Route path="planner" element={<Planner />} />
        <Route path="tracker" element={<Tracker />} />
      </Route>
    </Routes>
  );
}

export default App;
