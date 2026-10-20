"""Interface languages: lookup and fallback, the language files, and that interface text
stays out of the code."""
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from guitar_player import i18n

ROOT = Path(__file__).resolve().parents[1]
CJK = range(0x4e00, 0xa000)
# Chinese in code that is data, not interface text: track names that hint at a part, and
# the names settings were saved with before they were stored by id.
ALLOWED = {'guitar_player/arrange.py': {'旋律', '主音', '低音'},
           'guitar_player/prefs.py': {'霜白', '宣纸', '夜幕', '琥珀', '霓虹', '音符', '按键', '键盘'}}


def sources():
    yield from sorted((ROOT/'guitar_player').rglob('*.py'))
    yield ROOT/'launch.py'


def docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                yield body[0].value


def requested_keys(tree):
    """(keys, prefixes) passed to tr(): 'a.b', 'a.b' if x else 'a.c', 'a.'+x."""
    keys, prefixes = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'tr' and node.args:
            arg = node.args[0]
            options = [arg.body, arg.orelse] if isinstance(arg, ast.IfExp) else [arg]
            for option in options:
                if isinstance(option, ast.Constant):
                    keys.add(option.value)
                elif isinstance(option, ast.BinOp) and isinstance(option.left, ast.Constant):
                    prefixes.add(option.left.value)
    return keys, prefixes


class CodeTests(unittest.TestCase):
    def test_no_interface_text_in_the_code(self):
        found = []
        for path in sources():
            tree = ast.parse(path.read_text(encoding='utf-8'))
            skip = {id(node) for node in docstrings(tree)}
            name = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                        and any(ord(c) in CJK for c in node.value)
                        and node.value not in ALLOWED.get(name, ())):
                    found.append(f'{name}:{node.lineno}: {node.value[:40]}')
        self.assertEqual(found, [])

    def test_every_requested_key_is_in_the_source_language(self):
        source = i18n.entries(i18n.SOURCE)
        keys, prefixes = set(), set()
        for path in sources():
            k, p = requested_keys(ast.parse(path.read_text(encoding='utf-8')))
            keys |= k
            prefixes |= p
        self.assertEqual(sorted(keys - set(source)), [])
        for prefix in prefixes:
            self.assertTrue(any(k.startswith(prefix) for k in source), prefix)


class LanguageFileTests(unittest.TestCase):
    def test_built_in_files_match_the_source(self):
        source = i18n.entries(i18n.SOURCE)
        for path in sorted(i18n.BUILTIN.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertIsInstance(data['_meta']['name'], str, path.name)
            for key, text in data.items():
                if key != '_meta':
                    self.assertIn(key, source, path.name)
                    self.assertEqual(i18n.fields(text), i18n.fields(source[key]), (path.name, key))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(i18n.use, i18n.SOURCE)

    def write(self, code, data):
        (self.folder/f'{code}.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def test_missing_entries_fall_back_to_english_then_the_source(self):
        self.write('zh-CN', {'main.open': '打开', 'main.play': '演奏', 'main.stop': '停止'})
        self.write('en', {'_meta': {'name': 'English'}, 'main.open': 'Open MIDI', 'main.play': 'Play'})
        mock.patch.object(i18n, 'BUILTIN', self.folder/'none').start()
        self.addCleanup(mock.patch.stopall)
        self.write('fr', {'_meta': {'name': 'Français'}, 'main.open': 'Ouvrir un MIDI'})
        i18n.use('fr', self.folder)
        self.assertEqual(i18n.tr('main.open'), 'Ouvrir un MIDI')
        self.assertEqual(i18n.tr('main.play'), 'Play')
        self.assertEqual(i18n.tr('main.stop'), '停止')

    def test_an_entry_with_other_placeholders_is_ignored(self):
        self.write('en', {'status.exported': 'Exported {file}', 'view.fret': 'fret {fret}'})
        i18n.use('en', self.folder)
        self.assertEqual(i18n.tr('status.exported', name='a.mid'), '已导出 a.mid')
        self.assertEqual(i18n.tr('view.fret', fret=3), 'fret 3')

    def test_a_user_file_overrides_a_built_in_language(self):
        self.write('zh-CN', {'main.open': '打开曲谱'})
        i18n.use('zh-CN', self.folder)
        self.assertEqual(i18n.tr('main.open'), '打开曲谱')
        self.assertEqual(i18n.available(self.folder)['zh-CN'], '简体中文')

    def test_start_uses_the_saved_language_else_the_systems(self):
        self.write('pt-BR', {'_meta': {'name': 'Português (Brasil)'}})
        self.assertEqual(i18n.start('pt-BR', self.folder), 'pt-BR')
        self.assertEqual(i18n.start('xx', self.folder), i18n.match(i18n.system_language(), i18n.available(self.folder)))

    def test_matching_the_system_language(self):
        codes = ['zh-CN', 'en', 'pt-BR']
        self.assertEqual(i18n.match('pt-PT', codes), 'pt-BR')
        self.assertEqual(i18n.match('EN-us', codes), 'en')
        self.assertEqual(i18n.match('zh-TW', codes), 'zh-CN')
        self.assertEqual(i18n.match('ja-JP', codes), 'en')
        self.assertEqual(i18n.match('ja-JP', ['zh-CN']), 'zh-CN')
        codes = ['zh-CN', 'en', 'zh-TW']
        for wanted in ('zh-TW', 'zh-HK', 'zh-MO', 'zh-Hant-HK'):
            self.assertEqual(i18n.match(wanted, codes), 'zh-TW', wanted)
        for wanted in ('zh-CN', 'zh-SG', 'zh-Hans-CN'):
            self.assertEqual(i18n.match(wanted, codes), 'zh-CN', wanted)


if __name__ == '__main__':
    unittest.main()
