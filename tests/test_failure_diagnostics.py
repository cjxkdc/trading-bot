"""Failure evidence survives orchestration without SDK text or credentials."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.diagnostics import failure_details, diagnostic_fields
from app.portal_worker import dispatch
from brokers.opend_broker import OpenDBroker, OpenDSubmissionRejected, OpenDOrderIdentityError
from platforms.opend import OpenDError


class FailureDiagnosticsTests(unittest.TestCase):
    def test_unknown_exception_never_includes_text_attributes_or_external_paths(self):
        secret = 'PRIVATE-KEY-TOKEN-account-123456'
        exc = RuntimeError(secret)
        exc.payload = {'password': secret}
        result = failure_details(exc, secret)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result['phase'], 'module')
        self.assertEqual(result['exception_type'], 'RuntimeError')
        self.assertRegex(result['diagnostic_id'], r'^[0-9a-f]{32}$')

    def test_exact_local_reason_allowlist_rejects_appended_vendor_text(self):
        reason = 'OpenD order identity differs from the saved intent'
        self.assertEqual(failure_details(OpenDError(reason), 'recover')['error_code'], 'broker_order_identity_mismatch')
        result = failure_details(OpenDError(reason + ' token=SECRET'), 'recover')
        self.assertEqual(result['error_code'], 'module_operation_failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_code_locations_have_only_project_source_filename_line_function(self):
        broker = OpenDBroker.__new__(OpenDBroker)
        broker.symbol_map = {}
        try:
            broker._ticker('PRIVATE-SECURITY')
        except OpenDError as exc:
            result = failure_details(exc, 'execute')
        self.assertEqual(result['error_code'], 'instrument_mapping_missing')
        self.assertTrue(any(p['file'] == 'brokers/opend_broker.py' and p['function'] == '_ticker'
                            for p in result['code_locations']))
        self.assertNotIn('PRIVATE-SECURITY', json.dumps(result))
        self.assertNotIn(str(Path.cwd()), json.dumps(result))
        self.assertTrue(all(set(p) == {'file', 'line', 'function'} for p in result['code_locations']))

    def test_presend_cause_has_safe_reason_and_explicit_no_transmission_state(self):
        try:
            try:
                raise OpenDError('Insufficient USD cash including estimated fees')
            except OpenDError as cause:
                raise OpenDSubmissionRejected('PRIVATE INTERNAL MESSAGE') from cause
        except OpenDSubmissionRejected as exc:
            result = failure_details(exc, 'execute')
        self.assertEqual(result['error_code'], 'order_cash_insufficient')
        self.assertEqual(result['submission_state'], 'rejected_before_transmission')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_order_identity_diagnostics_expose_field_names_only(self):
        exc = OpenDOrderIdentityError(['remark', 'time_in_force', 'session', 'SECRET-token-value'])
        exc.payload = {'remark': 'SECRET-account-value'}
        result = failure_details(exc, 'recover')
        self.assertEqual(result['mismatched_fields'], ['remark', 'session', 'time_in_force'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(diagnostic_fields(result)['mismatched_fields'], result['mismatched_fields'])

    def test_portal_recovery_retains_blocked_semantics_and_diagnostics(self):
        with patch('app.portal_worker.perform', side_effect=OpenDError('OpenD order identity differs from the saved intent')):
            result = dispatch('recover')
        self.assertEqual(result['error_code'], 'broker_order_identity_mismatch')
        self.assertEqual((result['status'], result['recovery_state']), ('blocked', 'blocked'))
        self.assertFalse(result['automatic_retry'])
        self.assertEqual(result['phase'], 'recover')

    def test_proven_presend_rejection_immediately_pauses_automation(self):
        with patch('app.portal_worker.perform', side_effect=OpenDSubmissionRejected('PRIVATE')):
            result = dispatch('execute')
        self.assertEqual((result['status'], result['recovery_state']), ('paused', 'blocked'))
        self.assertEqual(result['submission_state'], 'rejected_before_transmission')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_automation_startup_failure_keeps_original_diagnostic_after_restart(self):
        import test_automation as fixtures
        case = fixtures.AutomationTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.auto.enable('paper-broker', 'data-provider', False, initialize=True)
        failure = dict(failure_details(OpenDError('OpenD connection failed; start and log in to the selected local gateway'), 'status'), status='blocked')
        case.results['status'] = failure
        result = case.auto.tick(case.worker)
        self.assertEqual(diagnostic_fields(result), diagnostic_fields(failure))
        saved = json.loads(case.auto.path.read_text(encoding='utf-8'))
        self.assertEqual(diagnostic_fields(saved['last_run']), diagnostic_fields(failure))
        self.assertFalse(saved['enabled'])

    def test_automation_recovery_failure_keeps_worker_evidence_in_last_run(self):
        import test_automation as fixtures
        case = fixtures.AutomationTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.enable()
        case.auto.tick(case.worker)
        case.results['execute'] = dict(status='paused', message='pending')
        case.next()
        failure = dict(failure_details(OpenDError('OpenD order identity differs from the saved intent'), 'recover'),
                       status='blocked', recovery_state='blocked')
        case.results['recover'] = failure
        result = case.next()
        self.assertEqual(diagnostic_fields(result), diagnostic_fields(failure))
        self.assertEqual(diagnostic_fields(case.auto.data['last_run']), diagnostic_fields(failure))
        self.assertFalse(case.auto.data['enabled'])

    def test_old_or_expired_cycle_reconciliation_keeps_diagnostics(self):
        import test_automation as fixtures
        for moment in ((2026, 10, 1, 11), (2026, 11, 2, 9)):
            with self.subTest(moment=moment):
                case = fixtures.AutomationTests()
                case.setUp()
                self.addCleanup(case.tearDown)
                case.enable()
                case.auto.tick(case.worker)
                case.results['execute'] = dict(status='paused', message='pending')
                case.next()
                case.now = fixtures.datetime(*moment, 40, tzinfo=fixtures.NY)
                failure = dict(failure_details(OpenDError('OpenD order identity differs from the saved intent'), 'reconcile'),
                               status='blocked', recovery_state='blocked')
                case.results['reconcile'] = failure
                result = case.auto.tick(case.worker)
                self.assertEqual(diagnostic_fields(result), diagnostic_fields(failure))
                self.assertEqual(diagnostic_fields(case.auto.data['last_run']), diagnostic_fields(failure))
                self.assertFalse(case.auto.data['enabled'])


if __name__ == '__main__':
    unittest.main()
