import React from 'react';
import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './routes/Dashboard';
import CollectionView from './routes/CollectionView';
import Settings from './routes/Settings';
import Groups from './routes/Groups';
import Logs from './routes/Logs';
import Planner from './routes/Planner';

function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="folder/:id" element={<CollectionView />} />
        <Route path="collection/:id" element={<CollectionView />} />
        <Route path="logs" element={<Logs />} />
        <Route path="settings" element={<Settings />} />
        <Route path="groups" element={<Groups />} />
        <Route path="groups/:groupId" element={<Groups />} />
        <Route path="planner" element={<Planner />} />
      </Route>
    </Routes>
  );
}

export default App;
