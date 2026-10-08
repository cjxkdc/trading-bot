import React, { useEffect, useRef } from 'react';
import { basicSetup } from 'codemirror';
import { EditorState, Compartment, Prec } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { python } from '@codemirror/lang-python';
import { HighlightStyle, indentUnit, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { indentWithTab, temporarilySetTabFocusMode } from '@codemirror/commands';

// Colours come from the page tokens, so the editor follows the dark/light theme switch.
const terminalHighlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.operatorKeyword, tags.definitionKeyword, tags.moduleKeyword], color: 'var(--code-keyword)' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--code-string)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--code-number)' },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: 'var(--code-comment)', fontStyle: 'italic' },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName), tags.definition(tags.function(tags.variableName))], color: 'var(--code-function)' },
  { tag: [tags.className, tags.typeName, tags.definition(tags.className)], color: 'var(--code-type)' },
  { tag: [tags.self, tags.special(tags.variableName)], color: 'var(--code-keyword)', fontStyle: 'italic' },
  { tag: [tags.operator, tags.punctuation], color: 'var(--secondary)' },
]);

// Reconfigure permissions without recreating the document, cursor or undo history
// on the Portal's regular state refreshes. The caller remounts for each file.
export function PythonEditor({ initialValue, readOnly, onChange, onSave }) {
  const host = useRef(null), view = useRef(null), permissions = useRef(new Compartment());
  const callbacks = useRef({ onChange, onSave, readOnly });
  callbacks.current = { onChange, onSave, readOnly };
  useEffect(() => {
    view.current = new EditorView({ parent: host.current, state: EditorState.create({
      doc: initialValue,
      extensions: [basicSetup, python(), indentUnit.of('    '), EditorState.tabSize.of(4),
        EditorState.phrases.of({ 'Find': '查找', 'Replace': '替换为', 'next': '下一个', 'previous': '上一个', 'all': '全选', 'match case': '区分大小写', 'regexp': '正则表达式', 'by word': '全字匹配', 'replace': '替换', 'replace all': '全部替换', 'close': '关闭搜索', 'Fold line': '折叠行', 'Unfold line': '展开行' }),
        permissions.current.of([EditorState.readOnly.of(readOnly), EditorView.contentAttributes.of({ 'aria-readonly': String(readOnly) })]),
        EditorView.contentAttributes.of({ 'aria-label': 'Python 策略代码', 'aria-describedby': 'editor-help', spellcheck: 'false' }),
        Prec.highest(keymap.of([{ key: 'Mod-s', preventDefault: true, run: () => { if (!callbacks.current.readOnly) callbacks.current.onSave(); return true; } }, indentWithTab])),
        Prec.low(keymap.of([{ key: 'Escape', run: temporarilySetTabFocusMode, stopPropagation: true }])),
        EditorView.updateListener.of(update => { if (update.docChanged) callbacks.current.onChange(update.state.doc.toString()); }),
        syntaxHighlighting(terminalHighlight),
        EditorView.theme({ '&': { height: '100%', backgroundColor: 'var(--surface-inset)', color: 'var(--ink)' }, '.cm-scroller': { overflow: 'auto', fontFamily: 'var(--font-mono)', fontSize: '13px', lineHeight: '1.65' }, '.cm-content': { padding: '12px 0', caretColor: 'var(--accent)' }, '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)' },
          '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': { backgroundColor: 'var(--accent-border) !important' }, '.cm-activeLine': { backgroundColor: 'var(--surface-hover)' }, '.cm-activeLineGutter': { backgroundColor: 'var(--surface-hover)', color: 'var(--ink)' },
          '.cm-gutters': { backgroundColor: 'var(--surface)', color: 'var(--faint)', borderRight: '1px solid var(--border)' }, '.cm-matchingBracket': { backgroundColor: 'var(--accent-soft)', outline: '1px solid var(--accent-border)' },
          '.cm-panels': { backgroundColor: 'var(--surface-raised)', color: 'var(--ink)' }, '.cm-panels.cm-panels-bottom': { borderTop: '1px solid var(--border)' }, '.cm-textfield': { backgroundColor: 'var(--field)', color: 'var(--ink)', border: '1px solid var(--border-strong)' },
          '.cm-button': { backgroundImage: 'none', backgroundColor: 'var(--surface-raised)', color: 'var(--ink)', border: '1px solid var(--border-strong)' }, '.cm-searchMatch': { backgroundColor: 'var(--warning-soft)', outline: '1px solid var(--warning-border)' },
          '.cm-tooltip': { backgroundColor: 'var(--surface-raised)', color: 'var(--ink)', border: '1px solid var(--border-strong)' }, '.cm-foldPlaceholder': { backgroundColor: 'var(--surface-raised)', color: 'var(--secondary)', border: '1px solid var(--border)' } }),
      ],
    }) });
    view.current.contentDOM.focus({ preventScroll: true });
    return () => { view.current.destroy(); view.current = null; };
  }, []);
  useEffect(() => { view.current?.dispatch({ effects: permissions.current.reconfigure([EditorState.readOnly.of(readOnly), EditorView.contentAttributes.of({ 'aria-readonly': String(readOnly) })]) }); }, [readOnly]);
  return <div className="python-editor" ref={host}/>;
}
