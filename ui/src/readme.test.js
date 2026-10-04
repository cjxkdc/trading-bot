import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReadmeMarkdown, readmeUrl } from './readme.js';

const render = content => renderToStaticMarkup(React.createElement(ReadmeMarkdown, { content }));

test('README renders headings, lists, tables and escaped code', () => {
  const html = render('# 策略\n\n- **规则**\n\n| 参数 | 数值 |\n| --- | --- |\n| count | 8 |\n\n```python\na < b\n```');
  assert.match(html, /<h1>策略<\/h1>/);
  assert.match(html, /<strong>规则<\/strong>/);
  assert.match(html, /<table>/);
  assert.match(html, /a &lt; b/);
});

test('README cannot execute HTML or load remote images', () => {
  const html = render('<script>alert(1)</script>\n\n<iframe src="https://evil.example"/>\n\n![示意](https://evil.example/track.png)');
  assert.doesNotMatch(html, /<script|<iframe|<img|src=/);
  assert.match(html, /示意/);
});

test('README links allow public HTTP(S) references and cannot call portal endpoints', () => {
  const html = render('[文档](https://example.com/reference) [本地](../docs/private.md) [接口](/api/runtime/stop) [脚本](javascript:alert)');
  assert.match(html, /href="https:\/\/example.com\/reference"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /target="_blank"/);
  assert.doesNotMatch(html, /href="(?:javascript:|\.\.\/|\/api\/)/);
  for (const url of ['file:///secrets', '//example.com', 'https://name:password@example.com', 'data:text/html,x'])
    assert.equal(readmeUrl(url), undefined);
});
