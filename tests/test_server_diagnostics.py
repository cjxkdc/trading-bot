import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from deploy.server.diagnose import inspect


class ServerDiagnosticTests(unittest.TestCase):
    @staticmethod
    def order(client, **changes):
        return dict(dict(client_id=client, symbol='FIGI:BBG000001', side='BUY', quantity='2', limit_price='10.50'), **changes)

    def framework(self, root, directory, records):
        folder = root/'runs'/directory
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/'journal.sqlite'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE batches (cycle TEXT); CREATE TABLE intents (id TEXT,cycle TEXT,body TEXT,status TEXT,state TEXT);')
            db.executemany('INSERT INTO intents VALUES (?,?,?,?,?)', [
                (client, 'private-batch', json.dumps(body), status, state) for client, body, status, state in records])
        return path

    def adapter(self, root, directory, records, filename='adapter.sqlite'):
        folder = root/'runs'/directory
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/filename
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE binding (identity_hash TEXT); CREATE TABLE orders (client_id TEXT,body TEXT,created_at TEXT,broker_id TEXT,state TEXT);')
            db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)', [
                (client, json.dumps(body), '2026-10-05T13:35:00+00:00', 'private-broker-id', state)
                for client, body, state in records])
        return path

    def test_read_only_summary_matches_journals_and_does_not_expose_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root/'runs/broker'
            runtime.mkdir(parents=True)
            journal = runtime/'journal.sqlite'
            adapter = runtime/'adapter.sqlite'
            with closing(sqlite3.connect(journal)) as db:
                db.executescript('CREATE TABLE batches (cycle TEXT); CREATE TABLE intents (id TEXT,cycle TEXT,body TEXT,status TEXT,state TEXT);')
                db.executemany('INSERT INTO intents VALUES (?,?,?,?,?)', [
                    ('private-order-id', 'private-batch', json.dumps(self.order('private-order-id')), 'unknown', None),
                    ('never-sent', 'private-batch', json.dumps(self.order('never-sent')), 'unknown', None),
                    ('planned', 'private-batch', json.dumps(self.order('planned')), 'planned', None)])
                db.commit()
            with closing(sqlite3.connect(adapter)) as db:
                db.executescript('CREATE TABLE binding (identity_hash TEXT); CREATE TABLE orders (client_id TEXT,body TEXT,created_at TEXT,broker_id TEXT,state TEXT);')
                db.execute('INSERT INTO orders VALUES (?,?,?,?,?)', ('private-order-id',
                    json.dumps(dict(self.order('private-order-id'), broker_symbol='AXTI')),
                    '2026-10-05T13:35:00+00:00','private-broker-id',None))
                db.commit()
            jobs = root/'runs/portal/jobs'; jobs.mkdir(parents=True)
            (jobs/'failure.json').write_text(json.dumps({'id':'private-job','action':'recover','status':'paused',
                'started_at':'2026-10-05T13:35:00+00:00','message':'api_key=secret-key','details':{'token':'secret-key'}}))
            before = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            output = inspect(root)
            self.assertEqual(output['orders']['unknown_without_adapter'],1)
            self.assertEqual(output['orders']['framework_count'],3)
            matched = next(x for x in output['orders']['recent_or_unresolved'] if x['adapter_match_count'])
            self.assertEqual(matched['ticker'],'AXTI')
            self.assertTrue(matched['broker_id_present'])
            dumped = json.dumps(output)
            for secret in ('private-order-id','private-batch','private-account','private-broker-id','private-job','secret-key'):
                self.assertNotIn(secret,dumped)
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_join_requires_same_account_directory_and_exact_intent_body(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            order = self.order('shared-id')
            self.framework(root, 'account-a', [('shared-id', order, 'unknown', None)])
            self.framework(root, 'account-b', [('shared-id', order, 'unknown', None)])
            self.adapter(root, 'account-b', [('shared-id', dict(order, broker_symbol='AXTI'), None)])
            report = inspect(root)['orders']
            self.assertEqual(sorted(r['adapter_match_status'] for r in report['recent_or_unresolved']), ['matched', 'missing'])
            self.assertEqual(report['unknown_without_adapter'], 1)
            self.assertEqual(len({r['account_ref'] for r in report['recent_or_unresolved']}), 2)
            self.adapter(root, 'account-a', [('shared-id', dict(order, quantity='999', broker_symbol='WRONG'), None)])
            report = inspect(root)['orders']
            mismatch = next(r for r in report['recent_or_unresolved'] if r['adapter_match_status'] == 'mismatched')
            self.assertEqual((mismatch['adapter_candidate_count'], mismatch['adapter_match_count']), (1, 0))
            self.assertNotIn('ticker', mismatch)
            self.assertEqual(report['unknown_without_adapter'], 0)
            self.assertEqual(report['unknown_unverified_adapter'], 1)

    def test_ambiguous_same_directory_candidates_never_enrich_even_with_one_exact_match(self):
        for other in (self.order('same', quantity='3'), None, ['private-payload']):
            with self.subTest(other_type=type(other).__name__), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                order = self.order('same')
                self.framework(root, 'broker', [('same', order, 'unknown', None)])
                self.adapter(root, 'broker', [('same', dict(order, broker_symbol='AXTI'), None)])
                self.adapter(root, 'broker', [('same', other, None)], filename='duplicate.sqlite')
                row = inspect(root)['orders']['recent_or_unresolved'][0]
                self.assertEqual((row['adapter_match_status'], row['adapter_candidate_count']), ('ambiguous', 2))
                self.assertNotIn('ticker', row)
                self.assertNotIn('broker_id_present', row)

    def test_non_object_json_and_mismatched_client_ids_count_unreadable_records_and_continue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            framework_records, adapter_records = [], []
            for i, invalid in enumerate((None, [], 'private-payload', 7, True)):
                client = 'malformed-' + str(i)
                framework_records.append((client, invalid, 'unknown', None))
                adapter_records.append((client, invalid, None))
            framework_records.extend([
                ('bad-state', self.order('bad-state'), 'unknown', 'null'),
                ('bad-client', self.order('different-client'), 'unknown', None),
                ('good', self.order('good'), 'unknown', None)])
            adapter_records.extend([
                ('bad-state', dict(self.order('bad-state'), broker_symbol='AXTI'), '[]'),
                ('bad-client', dict(self.order('different-client'), broker_symbol='AXTI'), None),
                ('good', dict(self.order('good'), broker_symbol='AXTI'), None)])
            self.framework(root, 'broker', framework_records)
            self.adapter(root, 'broker', adapter_records)
            before = {p:p.read_bytes() for p in root.rglob('*.sqlite')}
            report = inspect(root)['orders']
            self.assertEqual(report['framework_count'], 8)
            self.assertEqual(report['adapter_count'], 8)
            self.assertEqual(report['unreadable_records'], 14)
            self.assertEqual(report['unreadable_record_counts'], {'adapter': 7, 'framework': 7})
            self.assertEqual(sum(r['adapter_match_status'] == 'matched' for r in report['recent_or_unresolved']), 1)
            self.assertEqual(sum(not r['framework_record_readable'] for r in report['recent_or_unresolved']), 7)
            self.assertNotIn('private-payload', json.dumps(report))
            self.assertEqual(before, {p:p.read_bytes() for p in root.rglob('*.sqlite')})

    def test_intent_comparison_does_not_coerce_boolean_and_numeric_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.framework(root, 'broker', [('same', self.order('same', quantity=1), 'unknown', None)])
            self.adapter(root, 'broker', [('same', dict(self.order('same', quantity=True), broker_symbol='AXTI'), None)])
            row = inspect(root)['orders']['recent_or_unresolved'][0]
            self.assertEqual(row['adapter_match_status'], 'mismatched')
            self.assertNotIn('ticker', row)

    def test_incomplete_account_scan_cannot_claim_absence_or_a_unique_adapter_match(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.framework(root, 'broker', [('missing', self.order('missing'), 'unknown', None),
                                            ('present', self.order('present'), 'unknown', None)])
            self.adapter(root, 'broker', [('present', dict(self.order('present'), broker_symbol='AXTI'), None)])
            (root/'runs/broker/broken.sqlite').write_text('PRIVATE-SECRET')
            report = inspect(root)['orders']
            self.assertEqual(report['unknown_without_adapter'], 0)
            self.assertEqual(report['unknown_unverified_adapter'], 2)
            self.assertEqual(report['unreadable_databases'], 1)
            for row in report['recent_or_unresolved']:
                self.assertEqual(row['adapter_match_status'], 'unreadable')
                self.assertTrue(row['adapter_scan_incomplete'])
                self.assertNotIn('ticker', row)

    def test_unmatched_unknown_is_not_hidden_by_forty_recent_matched_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            completed = [('filled-' + str(i), self.order('filled-' + str(i)), 'filled', None) for i in range(60)]
            unresolved = [('unknown', self.order('unknown'), 'unknown', None),
                          ('cancelled', self.order('cancelled'), 'cancelled', None),
                          ('rejected', self.order('rejected'), 'rejected', None)]
            self.framework(root, 'broker', completed + unresolved)
            self.adapter(root, 'broker', [(client, dict(order, broker_symbol='AXTI'), None)
                                           for client, order, _, _ in completed])
            report = inspect(root)['orders']
            self.assertEqual(report['unknown_without_adapter'], 1)
            self.assertEqual(report['recent_or_unresolved'][0]['framework_status'], 'unknown')
            self.assertEqual({r['framework_status'] for r in report['recent_or_unresolved'][:3]}, {'unknown','cancelled','rejected'})
            self.assertEqual(report['truncated_count'], 23)
            self.assertEqual(report['unresolved_omitted'], 0)

    def test_unresolved_overflow_is_counted_and_unknown_precedes_planned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            planned = [('plan-' + str(i), self.order('plan-' + str(i)), 'planned', None) for i in range(45)]
            self.framework(root, 'broker', planned + [('unknown', self.order('unknown'), 'unknown', None)])
            report = inspect(root)['orders']
            self.assertEqual(report['recent_or_unresolved'][0]['framework_status'], 'unknown')
            self.assertEqual(report['truncated_count'], 6)
            self.assertEqual(report['unresolved_omitted'], 6)

    def test_corrupt_file_does_not_escape_as_raw_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'runs').mkdir()
            (root/'runs/bad.sqlite').write_text('secret-key')
            report = inspect(root)
            self.assertEqual(report['orders']['unreadable_databases'],1)
            self.assertNotIn('secret-key',json.dumps(report))


if __name__ == '__main__':
    unittest.main()
