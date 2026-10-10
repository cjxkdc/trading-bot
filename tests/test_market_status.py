"""Calendar-derived header status without broker calls or trading mutations."""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import patch

from app.market_status import selected_market_session
from framework.market import ExecutionContext


class MarketStatusTests(unittest.TestCase):
    def values(self, market='HK'):
        context=(ExecutionContext('HK','Asia/Hong_Kong','HKD','equity') if market=='HK'
                 else ExecutionContext.legacy_us())
        broker=dict(id='example-profile',enabled=True,execution_context=context.record())
        intervals=([{'open':'2026-10-08T09:30:00+08:00','close':'2026-10-08T12:00:00+08:00'},
                    {'open':'2026-10-08T13:00:00+08:00','close':'2026-10-08T16:00:00+08:00'}] if market=='HK' else
                   [{'open':'2026-10-08T09:30:00-04:00','close':'2026-10-08T13:00:00-04:00'}])
        automation=dict(binding={'broker_id':broker['id'],'context':'current'},config={'timezone':context.timezone},
                        calendar={'checked_day':'2026-10-08','sessions':[{'date':'2026-10-08','intervals':intervals}]})
        return broker, automation

    def status(self, at, broker, automation, instances=None, current='current', callback=None):
        return selected_market_session(broker,at=datetime.fromisoformat(at),current_context=current,
                                       automation=automation,instances=instances,connection_context=callback)

    def test_hong_kong_midday_break_comes_from_intervals(self):
        broker,automation=self.values()
        original=deepcopy(automation)
        status=self.status('2026-10-08T12:15:00+08:00',broker,automation)
        self.assertEqual(status['status'],'break')
        self.assertEqual(status['source'],'calendar_cache')
        self.assertEqual(status['observed_at'],'2026-10-08T12:15:00+08:00')
        self.assertEqual(automation,original)
        self.assertEqual(self.status('2026-10-08T13:00:00+08:00',broker,automation)['status'],'open')

    def test_us_early_close_does_not_use_a_fixed_sixteen_hour_close(self):
        broker,automation=self.values('US')
        self.assertEqual(self.status('2026-10-08T12:59:00-04:00',broker,automation)['status'],'open')
        self.assertEqual(self.status('2026-10-08T13:00:00-04:00',broker,automation)['status'],'closed')

    def test_absent_stale_and_legacy_date_only_caches_stay_unknown(self):
        broker,automation=self.values()
        for calendar in (None,{}, {'checked_day':'2026-10-07','sessions':automation['calendar']['sessions']},
                         {'checked_day':'2026-10-08','sessions':['2026-10-08']}):
            with self.subTest(calendar=calendar):
                self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,dict(automation,calendar=calendar)))

    def test_different_account_changed_binding_or_context_stays_unknown(self):
        broker,automation=self.values()
        for binding in ({'broker_id':'other','context':'current'}, {'broker_id':broker['id'],'context':'old'}, None):
            self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,dict(automation,binding=binding)))
        self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,automation,current=None))

    def test_missing_today_does_not_invent_a_holiday(self):
        broker,automation=self.values()
        automation['calendar']['sessions'][0]['date']='2026-10-09'
        automation['calendar']['sessions'][0]['intervals']=[
            {'open':'2026-10-09T09:30:00+08:00','close':'2026-10-09T16:00:00+08:00'}]
        self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,automation))

    def test_instance_cache_requires_recorded_connection_revision(self):
        broker,automation=self.values()
        cache={'day':'2026-10-08','sessions':automation['calendar']['sessions']}
        instances={'calendar':{broker['id']:cache}}
        def no_call(): self.fail('Unbound cache must not inspect another connection')
        self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,None,instances,callback=no_call))
        cache['connection_context']='current-connection'
        self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,None,instances,callback=lambda:'other'))
        result=self.status('2026-10-08T10:00:00+08:00',broker,None,instances,callback=lambda:'current-connection')
        self.assertEqual(result['status'],'open')

    def test_invalid_intervals_cannot_claim_the_market_is_open(self):
        broker,automation=self.values()
        automation['calendar']['sessions'][0]['intervals'][0]['open']='2026-10-08T09:30:00'
        self.assertIsNone(self.status('2026-10-08T10:00:00+08:00',broker,automation))


class PortalMarketStatusTests(unittest.TestCase):
    def test_state_exposes_cached_calendar_without_calling_worker(self):
        from tests.test_portal import PortalTests
        fixture=PortalTests(methodName='test_opening_portal_does_not_connect_or_create_a_trading_ledger')
        fixture.setUp()
        try:
            service=fixture.service
            self.assertIsNone(service.state()['market_session'])
            service.automation.data.update(binding={'broker_id':service.broker_id,
                'context':service.registry.context(service.broker_id,service.source)},
                calendar={'checked_day':'2026-10-08','sessions':[{'date':'2026-10-08','intervals':[
                    {'open':'2026-10-08T09:30:00-04:00','close':'2026-10-08T13:00:00-04:00'}]}]})
            with patch('app.workbench.now',return_value='2026-10-08T18:00:00+00:00'):
                status=service.state()['market_session']
            self.assertEqual(status['status'],'closed')
            self.assertEqual(status['broker_id'],service.broker_id)
            self.assertFalse(fixture.calls)
        finally: fixture.tearDown()


if __name__ == '__main__': unittest.main()
