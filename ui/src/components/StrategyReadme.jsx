import React, { useState } from 'react';
import { ReadmeMarkdown } from '../readme';

export function StrategyReadme({ content, filename, issue }) {
  const [open, setOpen] = useState(false);
  if (issue) return <p className="strategy-readme-issue small muted">{issue}</p>;
  if (typeof content !== 'string') return null;
  return <details className="strategy-readme" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>策略说明 <span>README</span></summary>
    {open && <div className="strategy-readme-body"><small className="readme-file muted">{filename}</small>
      <div className="readme-markdown"><ReadmeMarkdown content={content}/></div>
    </div>}
  </details>;
}
