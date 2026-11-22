import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../fonts';
import '../styles/tokens.css';
import '../styles/ui.css';
import './specimen.css';
import { Specimen } from './Specimen';
import { useLang } from '../i18n';

function Root() {
  const lang = useLang();
  return <Specimen key={lang} />;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
