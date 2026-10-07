"""Historical order labels use durable evidence, never a current security lookup."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from framework.journal_reader import JournalReader, JournalReadError


class JournalReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / 'runs' / 'account' / 'journal.sqlite'
        self.ledger = self.journal.with_name('futu-paper.sqlite')
        self.envelopes = self.root / 'portal' / 'execution'
        self.cycle = 'futu-paper:account-one:2026-10-06'
        self.order = dict(client_id='client-one', symbol='FIGI:BBG000001',
                          side='BUY', quantity='2', limit_price='10.50')

    def reader(self):
        return JournalReader(self.journal, broker_ledger_path=self.ledger,
                             envelope_dir=self.envelopes)

    def write_journal(self, orders=None, cycle=None):
        orders = orders or [self.order]
        cycle = cycle or self.cycle
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS batches (cycle TEXT PRIMARY KEY, fingerprint TEXT, body TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, cycle TEXT, body TEXT, status TEXT, state TEXT)')
            db.execute('INSERT INTO batches VALUES (?, ?, ?)', (cycle, 'frozen', json.dumps(orders)))
            for order in orders:
                db.execute('INSERT INTO intents VALUES (?, ?, ?, ?, NULL)',
                           (order['client_id'], cycle, json.dumps(order), 'planned'))

    def write_broker(self, ticker, order=None):
        order = order or self.order
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS orders (client_id TEXT PRIMARY KEY, body TEXT)')
            db.execute('INSERT INTO orders VALUES (?, ?)',
                       (order['client_id'], json.dumps(dict(order, broker_symbol=ticker))))

    def write_envelope(self, ticker='OLD', **changes):
        raw = dict(schema='paper_order_preparation_v1',
                   account={'account_id': 'futu-paper:account-one'},
                   execution_time='2026-10-06T17:30:00+00:00',
                   orders=[deepcopy(self.order)], instruments={self.order['symbol']: ticker})
        raw.update(changes)
        self.envelopes.mkdir(parents=True, exist_ok=True)
        path = self.envelopes / (str(len(list(self.envelopes.glob('*.json')))) + '.json')
        path.write_text(json.dumps(raw), encoding='utf-8')
        return path

    def test_missing_files_are_not_created(self):
        self.assertEqual(self.reader().recent()['items'], [])
        self.assertFalse(self.journal.parent.exists())
        self.assertFalse(self.envelopes.exists())

    def test_broker_label_preserves_internal_id_and_leaves_all_files_unchanged(self):
        self.write_journal()
        self.write_broker('AXTI')
        self.write_envelope('AXTI')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        item = self.reader().recent()['items'][0]
        self.assertEqual(item['asset_id'], 'FIGI:BBG000001')
        self.assertEqual(item['symbol'], item['asset_id'])
        self.assertEqual(item['ticker'], 'AXTI')
        self.assertEqual(item['display_symbol'], 'AXTI')
        self.assertEqual(item['ticker_source'], 'broker_ledger')
        self.assertEqual(item['ticker_status'], 'resolved')
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_planned_order_uses_entire_frozen_envelope_and_ignores_current_snapshot(self):
        self.write_journal()
        self.write_envelope('OLD')
        (self.root / 'current-snapshot.json').write_text(json.dumps(
            {'instruments': {self.order['symbol']: 'NEW'}}), encoding='utf-8')
        item = self.reader().recent()['items'][0]
        self.assertEqual(item['ticker'], 'OLD')
        self.assertEqual(item['ticker_source'], 'frozen_envelope')
        self.assertFalse(self.ledger.exists())

    def test_missing_historical_mapping_is_explicitly_an_internal_id(self):
        self.write_journal()
        item = self.reader().recent()['items'][0]
        self.assertIsNone(item['ticker'])
        self.assertIsNone(item['ticker_source'])
        self.assertEqual(item['ticker_status'], 'unavailable')
        self.assertEqual(item['display_symbol'], '内部标识：FIGI:BBG000001')

    def test_plain_internal_symbol_is_not_assumed_to_be_a_ticker(self):
        self.write_journal([dict(self.order, symbol='ABC')])
        item = self.reader().recent()['items'][0]
        self.assertIsNone(item['ticker'])
        self.assertEqual(item['display_symbol'], '内部标识：ABC')

    def test_same_client_id_with_different_broker_intent_is_not_used(self):
        self.write_journal()
        self.write_broker('WRONG', dict(self.order, quantity='3'))
        self.assertIsNone(self.reader().recent()['items'][0]['ticker'])

    def test_other_account_or_day_and_naive_timestamp_do_not_supply_mapping(self):
        self.write_journal()
        self.write_envelope('WRONG', account={'account_id': 'futu-paper:another-account'})
        self.write_envelope('WRONG', execution_time='2026-10-07T17:30:00+00:00')
        self.write_envelope('WRONG', execution_time='2026-10-06T17:30:00')
        self.assertIsNone(self.reader().recent()['items'][0]['ticker'])

    def test_matching_one_order_is_insufficient_when_whole_batch_differs(self):
        second = dict(self.order, client_id='client-two', symbol='FIGI:BBG000002')
        self.write_journal([self.order, second])
        self.write_envelope('WRONG')
        self.assertTrue(all(item['ticker'] is None for item in self.reader().recent()['items']))

    def test_envelope_matches_complete_batch_even_when_view_limit_is_one(self):
        second = dict(self.order, client_id='client-two')
        self.write_journal([self.order, second])
        self.write_envelope('OLD', orders=[self.order, second])
        items = self.reader().recent(limit=1)['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['ticker'], 'OLD')

    def test_conflicting_durable_mappings_do_not_choose_a_ticker(self):
        self.write_journal()
        self.write_envelope('OLD')
        self.write_broker('NEW')
        item = self.reader().recent()['items'][0]
        self.assertIsNone(item['ticker'])
        self.assertEqual(item['ticker_status'], 'conflict')
        self.assertEqual(item['display_symbol'], '内部标识：FIGI:BBG000001')

    def test_conflicting_envelopes_do_not_depend_on_file_order(self):
        self.write_journal()
        self.write_envelope('OLD')
        self.write_envelope('NEW')
        self.assertEqual(self.reader().recent()['items'][0]['ticker_status'], 'conflict')

    def test_invalid_optional_metadata_does_not_hide_orders(self):
        self.write_journal()
        self.ledger.write_bytes(b'not a database')
        path = self.write_envelope('OLD')
        path.write_text('{invalid', encoding='utf-8')
        items = self.reader().recent()['items']
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['ticker'])

    def test_invalid_ticker_is_not_displayed(self):
        self.write_journal()
        self.write_broker('FIGI:BBG000001')
        self.write_envelope('US.AXTI<script>')
        self.assertIsNone(self.reader().recent()['items'][0]['ticker'])

    def test_existing_intents_without_batch_metadata_remain_readable(self):
        self.write_journal()
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('DROP TABLE batches')
        self.write_envelope('OLD')
        self.assertIsNone(self.reader().recent()['items'][0]['ticker'])

    def test_invalid_primary_journal_is_reported(self):
        self.journal.parent.mkdir(parents=True)
        self.journal.write_bytes(b'not a database')
        with self.assertRaises(JournalReadError):
            self.reader().recent()

    def test_portal_reads_historical_label_without_credentials_or_connection(self):
        from app.workbench import PortalService
        from brokers.controls import OpenDPaperControl
        self.write_journal()
        self.write_broker('AXTI')
        entry = json.loads((Path(__file__).resolve().parents[1] /
                            'brokers/plugins/futu_paper.json').read_text(encoding='utf-8'))
        control = OpenDPaperControl(self.root,
                                   {'config_dir': 'secrets/futu', 'state_dir': 'runs/account'}, entry)
        service = SimpleNamespace(lock=threading.RLock(), broker_id='futu_paper',
                                  folder=self.envelopes.parent, registry=Mock())
        service.registry.control.return_value = control
        with patch('brokers.opend_broker.OpenDConnector._settings', side_effect=AssertionError('credentials read')), \
             patch('brokers.opend_broker.connection', side_effect=AssertionError('broker connection')):
            item = PortalService.orders(service)['items'][0]
        self.assertEqual(item['ticker'], 'AXTI')
        self.assertFalse((self.root / 'secrets').exists())


if __name__ == '__main__':
    unittest.main()
