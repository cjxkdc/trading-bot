"""Read-only monitoring, independent of the trading authorization switch."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import threading
import time
from uuid import uuid4

from app.control_contracts import ControlError
from app.notifications import NotificationSettings, request_json
from framework.events import EventStore
from framework.performance import PerformanceStore


class Monitoring:
    def __init__(self, root, registry):
        self.registry = registry
        self.settings = NotificationSettings(root)
        self.events = EventStore(root/'runs/monitor/events.sqlite')
        self.performance = PerformanceStore(root/'runs/monitor/performance.sqlite')
        self.next_observation = 0
        self.next_heartbeat = 0
        self.last_check = None
        self.last_success = None
        self.notification_thread = None
        self.error = None

    def scope(self, broker):
        if 'observe' not in self.registry.descriptor('broker', broker)['capabilities']:
            raise ControlError('此账户模块尚未提供账户表现接口。')
        return self.registry.control('broker',broker).financial_account_reference()

    def emit(self, key, message, severity='info', next_action=None, **kwargs):
        return self.events.emit(key,message,severity,next_action,
                                notify=self.settings.config['channel']!='none',**kwargs)

    def observed(self, broker, result, expected_scope):
        self.last_check = datetime.now(timezone.utc).isoformat()
        valid = result.get('status') == 'complete' and isinstance(result.get('observation'),dict)
        if valid:
            item = result['observation']
            if self.scope(broker) != expected_scope or item.get('account_ref') != expected_scope:
                raise ControlError('检查期间账户绑定改变，未合并账户历史。')
            self.performance.record(expected_scope,item)
            self.last_success, self.error = self.last_check, None
            self.events.transition('monitor-config:'+broker,True,'账户监控配置已恢复。','positions',
                notify=self.settings.config['channel']!='none')
        else:
            self.error = '账户监控未取得最新估值，请检查券商登录和连接。'
        self.events.transition('account:'+expected_scope, valid,
            '账户监控已恢复。' if valid else self.error, 'positions' if valid else 'broker',
            notify=self.settings.config['channel']!='none')
        self.next_observation = time.time()+self.settings.config['interval_seconds']

    def cycle(self, broker, result, strategy):
        if result.get('decision_id') and 'targets' in result:
            self.performance.cycle(self.scope(broker), result, strategy=strategy)

    def automation_events(self, events):
        for event in events:
            key = 'automation:'+sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
            severity = event.get('severity','info')
            self.emit(key,event['message'],severity,'logs',at=event['at'])

    def public(self):
        return dict(settings=self.settings.public(), last_check=self.last_check,
                    last_success=self.last_success, error=self.error, events=self.events.recent(12))

    def configure(self, value):
        self.settings.save(value)
        # Never replay an old queue to a newly selected destination.
        self.events.cancel_pending()
        self.next_observation = self.next_heartbeat = 0
        return {'message':'监控设置已保存。'}

    def test_notification(self):
        if self.settings.config['channel']=='none':
            raise ControlError('请先保存通知渠道。')
        self.emit('test:'+uuid4().hex,'这是一条工作台测试通知。')
        return {'message':'测试通知已入队，可在事件记录查看送达状态。'}

    def pump(self):
        # Network I/O must not hold Portal locks or delay the trading scheduler.
        if self.notification_thread and self.notification_thread.is_alive():
            return
        def work():
            try:
                if self.settings.config['channel'] != 'none':
                    self.events.deliver_one(self.settings.send)
                url = self.settings.config['heartbeat_url']
                if url and time.time() >= self.next_heartbeat:
                    self.next_heartbeat = time.time()+60
                    try:
                        request_json(url)
                        ok = True
                    except Exception:
                        ok = False
                    self.events.transition('external-heartbeat',ok,
                        '外部心跳已恢复。' if ok else '外部心跳发送失败，请检查服务器网络。','logs')
            except Exception:
                self.error = '通知记录暂不可用，请检查服务器磁盘与运行日志。'
        self.notification_thread = threading.Thread(target=work,name='monitor-notifications',daemon=True)
        self.notification_thread.start()
