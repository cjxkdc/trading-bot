import React from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Documentation can link to public references; it cannot execute HTML, load
// remote images, or turn a relative repository path into a control endpoint.
export function readmeUrl(url) {
  if (!/^https?:\/\//i.test(url)) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.username || parsed.password ? undefined : parsed.href;
  } catch { return undefined; }
}

const components = {
  a: ({ href, children, title }) => href
    ? React.createElement('a', { href, title, target: '_blank', rel: 'noopener noreferrer' }, children)
    : React.createElement('span', { title }, children),
  img: ({ alt }) => alt ? React.createElement('span', { className: 'muted' }, alt) : null,
  table: ({ children }) => React.createElement('div', { className: 'readme-table' },
    React.createElement('table', null, children)),
};

export const ReadmeMarkdown = React.memo(function ReadmeMarkdown({ content }) {
  return React.createElement(Markdown, {
    remarkPlugins: [remarkGfm], skipHtml: true, urlTransform: readmeUrl, components,
  }, content);
});
