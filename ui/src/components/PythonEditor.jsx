import React, { useEffect, useRef } from 'react';
import { basicSetup } from 'codemirror';
import { EditorState, Compartment, Prec } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { python } from '@codemirror/lang-python';
import { indentUnit } from '@codemirror/language';
import { indentWithTab, temporarilySetTabFocusMode } from '@codemirror/commands';

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
        EditorView.theme({ '&': { height: '100%' }, '.cm-scroller': { overflow: 'auto', fontFamily: "'Cascadia Code', Consolas, monospace", fontSize: '13px', lineHeight: '1.65' }, '.cm-content': { padding: '12px 0' }, '.cm-gutters': { backgroundColor: 'var(--surface-inset)', color: 'var(--secondary)', borderRight: '1px solid var(--border)' } }),
      ],
    }) });
    view.current.focus();
    return () => { view.current.destroy(); view.current = null; };
  }, []);
  useEffect(() => { view.current?.dispatch({ effects: permissions.current.reconfigure([EditorState.readOnly.of(readOnly), EditorView.contentAttributes.of({ 'aria-readonly': String(readOnly) })]) }); }, [readOnly]);
  return <div className="python-editor" ref={host}/>;
}
