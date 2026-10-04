"""Strategy-owned README display cannot expose files outside the module."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from app.module_registry import ModuleRegistry
from app.strategy_documentation import MAX_README_BYTES, strategy_documentation

ROOT = Path(__file__).resolve().parents[1]


class StrategyDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'strategies'
        self.directory.mkdir()
        shutil.copytree(ROOT / 'strategies/plugins', self.directory / 'plugins')
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets/key.md').write_text('PRIVATE-SECRET', encoding='utf-8')

    def descriptor(self, ident='mom8'):
        return next(s for s in ModuleRegistry(self.root).strategies() if s['type'] == ident)

    def test_optional_and_missing_documentation_do_not_disable_strategy(self):
        self.assertTrue(self.descriptor()['enabled'])
        self.assertIsNone(self.descriptor()['readme'])
        self.assertIn('readme_issue', self.descriptor())
        other = self.descriptor('equal_weight')
        self.assertTrue(other['enabled'])
        self.assertIsNone(other['readme'])
        self.assertNotIn('readme_issue', other)

    def test_nested_strategy_readme_is_discovered_without_a_ui_or_framework_edit(self):
        entry = self.directory / 'plugins/mom8.json'
        value = json.loads(entry.read_text(encoding='utf-8'))
        value['readme'] = 'mom8_docs/README.md'
        entry.write_text(json.dumps(value), encoding='utf-8')
        (self.directory / 'mom8_docs').mkdir()
        (self.directory / 'mom8_docs/README.md').write_text('# 独立说明\n\n**规则**', encoding='utf-8-sig')
        doc = self.descriptor()['readme']
        self.assertEqual(doc, {'path': 'mom8_docs/README.md', 'content': '# 独立说明\n\n**规则**'})
        self.assertTrue(self.descriptor()['enabled'])

    def test_private_absolute_parent_and_non_markdown_paths_are_rejected(self):
        references = ['../secrets/key.md', str(self.root / 'secrets/key.md'),
                      'C:/secrets/key.md', '..\\secrets\\key.md', 'mom8.py', 42, '', 'bad\0.md']
        for reference in references:
            with self.subTest(reference=reference):
                value = strategy_documentation(self.directory, reference)
                self.assertIsNone(value['readme'])
                self.assertNotIn('PRIVATE-SECRET', json.dumps(value))
                self.assertIn('readme_issue', value)

    def test_symlink_cannot_read_private_files(self):
        try:
            (self.directory / 'linked.md').symlink_to(self.root / 'secrets/key.md')
        except OSError:
            self.skipTest('Symlinks require privileges on this Windows installation')
        value = strategy_documentation(self.directory, 'linked.md')
        self.assertIsNone(value['readme'])
        self.assertNotIn('PRIVATE-SECRET', json.dumps(value))

    def test_oversized_and_invalid_utf8_documents_are_isolated(self):
        for content in (b'x' * (MAX_README_BYTES + 1), b'\xff', b'text\0binary'):
            (self.directory / 'MOM8.md').write_bytes(content)
            value = self.descriptor()
            self.assertTrue(value['enabled'])
            self.assertIsNone(value['readme'])
            self.assertIn('readme_issue', value)

    def test_document_edits_refresh_without_changing_execution_defaults(self):
        path = self.directory / 'MOM8.md'
        path.write_text('# Version one', encoding='utf-8')
        before = self.descriptor()
        path.write_text('# Version two', encoding='utf-8')
        after = self.descriptor()
        self.assertNotEqual(before['readme'], after['readme'])
        self.assertEqual(before['defaults'], after['defaults'])
        self.assertEqual(before['fields'], after['fields'])


if __name__ == '__main__':
    unittest.main()
