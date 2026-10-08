import React from 'react';
import { createRoot } from 'react-dom/client';
import { PortalAccess } from './login';
import { App } from './app/App';
import { applyPreferences } from './preferences';
import './style.css';

applyPreferences();

createRoot(document.getElementById('root')).render(
  <PortalAccess>{(access, logout) => <App access={access} onLogout={logout} />}</PortalAccess>,
);
