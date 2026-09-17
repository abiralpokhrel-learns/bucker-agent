import React from 'react';
import {createRoot} from 'react-dom/client';
import {DesktopApp} from './desktop/DesktopApp';
import './desktop/desktop.css';

const container = document.getElementById('root');
if (container) createRoot(container).render(<DesktopApp />);
