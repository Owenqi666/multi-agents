import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../fonts';
import '../styles/tokens.css';
import '../styles/ui.css';
import './specimen.css';
import { Specimen } from './Specimen';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Specimen />
  </StrictMode>,
);
