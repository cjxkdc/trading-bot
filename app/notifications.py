"""Replaceable outbound transports. Never accept trade commands or inbound webhooks."""
import json
import os
from pathlib import Path
import re
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import uuid4

from app.control_contracts import ControlError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(url, payload=None, *, opener=None):
    opener = opener or build_opener(ProxyHandler({}), NoRedirect())
    request = Request(url, data=json.dumps(payload or {}).encode(), headers={'Content-Type':'application/json'}, method='POST')
    with opener.open(request, timeout=8) as reply:
        if reply.geturl() != url or not 200 <= reply.status < 300:
            raise ValueError('Notification not acknowledged')
        raw = reply.read(65537)
        if len(raw) > 65536:
            raise ValueError('Notification response exceeds limit')
        return raw


class Telegram:
    label = 'Telegram'
    fields = [dict(name='telegram_token',label='Bot Token',secret=True),
              dict(name='telegram_chat_id',label='Chat ID',secret=True)]
    @staticmethod
    def validate(cfg):
        if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', cfg.get('telegram_token','')):
            raise ControlError('请填写有效的 Telegram Bot Token。')
        if not re.fullmatch(r'-?[0-9]{1,20}', cfg.get('telegram_chat_id','')):
            raise ControlError('请填写数字格式的 Telegram Chat ID。')

    @staticmethod
    def send(cfg, event):
        text = f"Trading Bot · {event['severity']}\n{event['message']}\n{event['at']}\n事件 {event['id'][:8]}"
        result = json.loads(request_json('https://api.telegram.org/bot'+cfg['telegram_token']+'/sendMessage',
            dict(chat_id=cfg['telegram_chat_id'], text=text, link_preview_options={'is_disabled':True})))
        if result.get('ok') is not True:
            raise ValueError('Notification not acknowledged')


TRANSPORTS = {'telegram': Telegram}
CHANNEL_FIELDS = {field['name'] for transport in TRANSPORTS.values() for field in transport.fields}
DEFAULTS = dict(enabled=True, interval_seconds=900, channel='none', heartbeat_url='',
                **{name:'' for name in CHANNEL_FIELDS})


class NotificationSettings:
    def __init__(self, root):
        self.path = Path(root)/'secrets/notifications/connection.json'
        self.config = dict(DEFAULTS)
        self.corrupt = False
        if self.path.exists():
            try:
                if self.path.stat().st_size > 16384:
                    raise ValueError('Settings too large')
                self.config = self.validate(dict(DEFAULTS, **json.loads(self.path.read_text(encoding='utf-8'))))
            except Exception:
                self.config = dict(DEFAULTS, enabled=False)
                self.corrupt = True

    @staticmethod
    def validate(cfg):
        if set(cfg) != set(DEFAULTS) or type(cfg['enabled']) is not bool:
            raise ControlError('监控配置字段无效。')
        if type(cfg['interval_seconds']) is not int or not 300 <= cfg['interval_seconds'] <= 3600:
            raise ControlError('账户检查间隔应为 5–60 分钟。')
        if cfg['channel'] not in {'none', *TRANSPORTS}:
            raise ControlError('通知渠道尚未注册。')
        if cfg['channel'] != 'none':
            TRANSPORTS[cfg['channel']].validate(cfg)
        if cfg['heartbeat_url'] and not re.fullmatch(r'https://hc-ping\.com/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', cfg['heartbeat_url']):
            raise ControlError('离线心跳仅接受 Healthchecks 的 HTTPS UUID 地址。')
        return cfg

    def save(self, value):
        if not isinstance(value,dict) or set(value)-set(DEFAULTS)-{'clear_heartbeat','clear_telegram'}:
            raise ControlError('监控配置包含未知字段。')
        cfg = dict(self.config)
        for key, item in value.items():
            if key in DEFAULTS and (key not in {*CHANNEL_FIELDS,'heartbeat_url'} or item):
                cfg[key] = item.strip() if isinstance(item,str) else item
        if value.get('clear_heartbeat') is True:
            cfg['heartbeat_url']=''
        if value.get('clear_telegram') is True:
            cfg.update(channel='none',telegram_token='',telegram_chat_id='')
        cfg = self.validate(cfg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.'+uuid4().hex+'.tmp')
        try:
            with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as handle:
                json.dump(cfg,handle,ensure_ascii=False)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.config, self.corrupt = cfg, False

    def public(self):
        return {k:self.config[k] for k in ('enabled','interval_seconds','channel')} | dict(
            telegram_configured=bool(self.config['telegram_token'] and self.config['telegram_chat_id']),
            heartbeat_configured=bool(self.config['heartbeat_url']), configuration_error=self.corrupt,
            channels=[dict(id=key,name=value.label,fields=value.fields,
                           configured=all(self.config.get(field['name']) for field in value.fields))
                      for key,value in TRANSPORTS.items()])

    def send(self, event):
        cfg = dict(self.config)
        if cfg['channel'] == 'none':
            raise ControlError('通知渠道尚未配置。')
        TRANSPORTS[cfg['channel']].send(cfg,event)
