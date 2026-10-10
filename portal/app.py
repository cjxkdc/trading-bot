"""Loopback-only HTTP surface. Never exposes project files or arbitrary commands."""
import json
import logging
from math import isfinite
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.workbench import MAX_FILE, PortalError, PortalService
from app.execution_policy import CURRENT_EXECUTION_POLICY, load_execution_policy
from app.logs import incident_id, log_exception


class LocalOnly:
    def __init__(self, app, *, port, token):
        self.app, self.token = app, token
        self.hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        self.origins = {'http://' + h for h in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        origin = headers.get(b'origin', b'').decode('latin1')
        site = headers.get(b'sec-fetch-site', b'').decode('latin1')
        path = scope['path']
        error = None
        if host not in self.hosts or (origin and origin not in self.origins) or (
                site not in {'', 'same-origin', 'none'} and path != '/api/broker-auth/callback'):
            error = (403, '仅允许本机面板访问。')
        elif path.startswith('/api/') and path not in {'/api/session', '/api/auth/status', '/api/broker-auth/callback'} and not secrets.compare_digest(
                headers.get(b'x-portal-token', b''), self.token.encode()):
            error = (403, '面板会话已更新，请刷新页面。')
        elif scope['method'] in {'POST', 'PUT', 'PATCH'}:
            try:
                too_large = int(headers.get(b'content-length', b'0')) > MAX_FILE
            except ValueError:
                too_large = True
            if too_large:
                error = (413, '文件超过 32 MB。')
            else:
                # Enforce the same bound for chunked bodies, before any endpoint parses JSON.
                data = bytearray()
                while True:
                    part = await receive()
                    if part['type'] == 'http.disconnect':
                        return
                    data.extend(part.get('body', b''))
                    if len(data) > MAX_FILE:
                        error = (413, '文件超过 32 MB。')
                        break
                    if not part.get('more_body', False):
                        break
                sent = False
                original_receive = receive

                async def buffered():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {'type': 'http.request', 'body': bytes(data), 'more_body': False}
                    return await original_receive()
                receive = buffered

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).extend([
                    (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    (b'referrer-policy', b'no-referrer'), (b'x-frame-options', b'DENY'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")])
            await send(message)

        if error:
            return await JSONResponse({'message': error[1]}, status_code=error[0])(scope, receive, secure_send)
        return await self.app(scope, receive, secure_send)


async def body(request):
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise PortalError('请求必须使用 JSON。')
    try:
        value = await request.json()
    except (ValueError, UnicodeError):
        raise PortalError('JSON 格式无效。') from None
    if not isinstance(value, dict):
        raise PortalError('请求格式无效。')
    return value


def create_app(root, *, port=8765, service=None, instance='development', shutdown=None):
    service = service or PortalService(root)
    token = secrets.token_urlsafe(32)
    @asynccontextmanager
    async def lifespan(app):
        service.start_scheduler()
        try:
            yield
        finally:
            service.stop_scheduler()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.portal = service
    app.state.portal_token = token
    app.add_middleware(LocalOnly, port=port, token=token)

    log = logging.getLogger('trading_bot.backend')

    @app.exception_handler(PortalError)
    async def portal_error(request, exc):
        return JSONResponse({'message': str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    @app.exception_handler(TypeError)
    @app.exception_handler(KeyError)
    async def invalid_input(request, exc):
        # Also raised by genuine defects; the incident id points at the traceback
        # in runs/portal/logs/backend.log so they are no longer invisible.
        ident = incident_id()
        log_exception(log, f'422 {request.method} {request.url.path}', exc, ident)
        return JSONResponse({'message': f'数据或配置未通过检查，请核对格式、日期及参数。（诊断编号 {ident}）',
                             'incident_id': ident}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        ident = incident_id()
        log_exception(log, f'500 {request.method} {request.url.path}', exc, ident)
        return JSONResponse({'message': f'本地操作失败，原有账本和配置已保留。（诊断编号 {ident}）',
                             'incident_id': ident}, status_code=500)

    @app.get('/health')
    def health():
        return {'application': 'trading-bot-portal', 'instance': instance,
                'execution_policy': CURRENT_EXECUTION_POLICY.identifier,
                'execution_scope': load_execution_policy(root).public()}

    @app.get('/api/session')
    def session():
        return {'token': token}

    @app.get('/api/state')
    def state():
        return service.state()

    @app.post('/api/strategy-instances')
    async def create_instance(request: Request):
        return await run_in_threadpool(service.strategy_instance_action, 'create', await body(request))

    @app.post('/api/strategy-instances/limits')
    async def strategy_limits(request: Request):
        return await run_in_threadpool(service.strategy_instance_action, 'limits', await body(request))

    @app.post('/api/strategy-instances/{ident}')
    async def update_instance(ident: str, request: Request):
        return await run_in_threadpool(service.strategy_instance_action, 'update', await body(request), ident)

    @app.get('/api/strategy-instances/{ident}/logs')
    def instance_logs(ident: str):
        return service._public({'items': service.strategy_instances.logs(ident)})

    @app.post('/api/strategy-instances/{ident}/export')
    async def instance_export(ident: str, request: Request):
        return await run_in_threadpool(service.export_instance, ident, await body(request))

    @app.post('/api/strategy-instances/{ident}/{action}')
    async def instance_action(ident: str, action: str, request: Request):
        value = await body(request)
        if action == 'resolve':
            if value != {'confirmation': ident}:
                raise PortalError('请确认结束未提交余单；已成交持仓会保留。')
            return await run_in_threadpool(service.strategy_instance_job, ident, 'strategy-resolve')
        if action == 'close-terminal':
            if value != {'confirmation': ident}:
                raise PortalError('请确认已在券商端核对撤单或拒单，再关闭这些委托。')
            return await run_in_threadpool(service.strategy_instance_job, ident, 'strategy-close-terminal')
        if action in {'check', 'preview', 'reconcile'}:
            if value:
                raise PortalError('检查不接受订单参数。')
            return await run_in_threadpool(service.strategy_instance_job, ident,
                                           {'check':'strategy-check', 'preview':'strategy-preview',
                                            'reconcile':'strategy-reconcile'}[action])
        return await run_in_threadpool(service.strategy_instance_action, action, value, ident)

    @app.post('/api/strategy-plugins/upload')
    async def upload_strategy(request: Request):
        return await run_in_threadpool(service.upload_strategy, await request.body())

    @app.get('/api/strategy-plugins/{strategy_type}/source')
    def strategy_source(strategy_type: str):
        return service.strategy_source(strategy_type)

    @app.post('/api/strategy-plugins/{strategy_type}/source')
    async def save_strategy_source(strategy_type: str, request: Request):
        return await run_in_threadpool(service.strategy_source, strategy_type, await body(request))

    @app.get('/api/strategy-plugins/{strategy_type}/bundle')
    def strategy_bundle(strategy_type: str):
        return Response(service.strategy_files.bundle(strategy_type), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="strategy.zip"'})

    @app.get('/api/orders')
    def orders(broker: str | None = None):
        return service.orders(broker)

    @app.get('/api/performance')
    def performance(broker: str | None = None):
        return service.performance(broker)

    @app.get('/api/performance/candles')
    def performance_candles(interval: str = '1h', limit: int = 96, account_key: str | None = None, broker: str | None = None):
        return service.performance_candles(interval, limit, account_key, broker)

    # Performance writes take the service lock and recompute reports; keep them
    # off the event loop so status polling is never blocked behind them.
    @app.post('/api/performance/cash-event')
    async def cash_event(request: Request):
        return await run_in_threadpool(service.performance_cash_event, await body(request))

    @app.post('/api/performance/review')
    async def review_performance(request: Request):
        return await run_in_threadpool(service.performance_review, await body(request))

    @app.post('/api/performance/cash-event/remove')
    async def remove_cash_event(request: Request):
        return await run_in_threadpool(service.performance_remove_event, await body(request))

    @app.get('/api/monitoring/events')
    def monitoring_events():
        return service.monitor.events.recent()

    # Every call below takes the service lock (and may do file or network I/O);
    # running them in the threadpool keeps the event loop, and therefore the
    # 2.5-second status polling, responsive while another thread holds it.
    @app.post('/api/monitoring/config')
    async def monitoring_config(request: Request):
        return await run_in_threadpool(service.monitoring_configure, await body(request))

    def _test_notification():
        with service.lock:
            return service.monitor.test_notification()

    @app.post('/api/monitoring/test')
    async def monitoring_test(request: Request):
        if await body(request):
            raise PortalError('测试通知不接受消息内容。')
        return await run_in_threadpool(_test_notification)

    @app.post('/api/automation/config')
    async def automation_config(request: Request):
        return await run_in_threadpool(service.automation_configure, await body(request))

    @app.post('/api/automation/enable')
    async def automation_enable(request: Request):
        if await body(request):
            raise PortalError('自动运行只能使用当前选定并已核验的模块。')
        return await run_in_threadpool(service.automation_enable)

    @app.post('/api/automation/disable')
    async def automation_disable(request: Request):
        if await body(request):
            raise PortalError('暂停不接受订单参数。')
        return await run_in_threadpool(service.automation_disable)

    @app.post('/api/source')
    async def source(request: Request):
        value = await body(request)
        return await run_in_threadpool(service.select_source, value.get('source'))

    @app.post('/api/broker')
    async def broker(request: Request):
        value = await body(request)
        return await run_in_threadpool(service.select_broker, value.get('broker'))

    @app.post('/api/broker/settings')
    async def broker_settings(request: Request):
        return await run_in_threadpool(service.configure_broker, await body(request))

    @app.post('/api/broker/disconnect')
    async def disconnect(request: Request):
        if await body(request):
            raise PortalError('断开账户不接受交易参数。')
        return await run_in_threadpool(service.disconnect_broker)

    @app.post('/api/broker-auth/start')
    async def authorize(request: Request):
        value = await body(request)
        if set(value) != {'callback_origin'}:
            raise PortalError('网页授权请求格式无效。')
        # The controller supplies its configured origin. Direct desktop instances
        # accept only the LocalOnly Host already checked by their middleware.
        from app.origins import validate_origin
        origin = validate_origin(value['callback_origin'])
        return await run_in_threadpool(service.begin_authorization, origin + '/api/broker-auth/callback')

    @app.get('/api/broker-auth/callback')
    async def authorized(request: Request):
        values = request.query_params
        success = len(values.getlist('code')) == 1 and not values.getlist('error')
        denied = len(values.getlist('error')) == 1 and not values.getlist('code')
        if len(values.getlist('state')) != 1 or not (success or denied):
            raise PortalError('网页授权回调格式无效，请重新开始。')
        try:
            await run_in_threadpool(service.finish_authorization, values.get('state'), values.get('code', ''))
        except PortalError:
            pass  # No code/state/token is echoed; the Portal shows a fixed notice.
        return RedirectResponse('/', status_code=303)

    @app.post('/api/broker-auth/bind')
    async def bind(request: Request):
        value = await body(request)
        if set(value) != {'account'}:
            raise PortalError('请选择返回的账户。')
        return await run_in_threadpool(service.bind_authorization, value['account'])

    @app.post('/api/strategy')
    async def strategy(request: Request):
        value = await body(request)
        return await run_in_threadpool(service.change_strategy, value.get('strategy'), value.get('version'))

    @app.post('/api/jobs')
    async def job(request: Request):
        value = await body(request)
        action = value.get('action')
        if action == 'configure-data':
            raise PortalError('请在数据设置中保存密钥。')
        if action == 'execute' and value.get('confirmation') != service.broker_id:
            raise PortalError('请先核对预览并确认当前账户模块。')
        if action == 'resolve-remainder' and value.get('confirmation') != service.broker_id:
            raise PortalError('请明确确认当前账户，并确认结束尚未提交的余单。')
        if action == 'close-terminal' and value.get('confirmation') != service.broker_id:
            raise PortalError('请明确确认当前账户，并确认已在券商端核对撤单或拒单。')
        if action == 'close-untransmitted' and value.get('confirmation') != service.broker_id:
            raise PortalError('请明确确认当前账户，并确认已在券商端核对这些委托从未送达。')
        return await run_in_threadpool(service.start, action)

    @app.post('/api/data/settings')
    async def data_settings(request: Request):
        value = await body(request)
        fields = {f['name']: f for f in service.registry.descriptor('data', service.source)['fields']}
        if set(value) - fields.keys():
            raise PortalError('提交的设置不属于当前数据模块。')
        if any(f.get('required') and (name not in value or value[name] in ('', None)) for name, f in fields.items()):
            raise PortalError('请填写此模块要求的设置。')
        for name, item in value.items():
            field = fields[name]
            if field['type'] in {'number', 'integer'}:
                if (type(item) not in (int, float) or not isfinite(item) or field['type'] == 'integer' and type(item) is not int
                        or item < field.get('min', float('-inf')) or item > field.get('max', float('inf'))):
                    raise PortalError('数值设置超出模块允许的范围。')
            elif field['type'] == 'boolean':
                if type(item) is not bool:
                    raise PortalError('开关设置必须是布尔值。')
            elif field['type'] == 'json':
                try:
                    if len(json.dumps(item, allow_nan=False)) > field.get('max_length', 4096):
                        raise ValueError('size')
                except (ValueError, TypeError):
                    raise PortalError('结构化设置格式无效或超过长度限制。') from None
            elif not isinstance(item, str) or len(item) > field.get('max_length', 4096):
                raise PortalError('文本设置格式无效。')
        return await run_in_threadpool(service.start, 'configure-data', credentials=value)

    @app.post('/api/cancel-download')
    def cancel():
        return service.cancel_download()

    @app.post('/api/snapshot')
    async def snapshot(request: Request):
        return await run_in_threadpool(service.upload_snapshot, await request.body())

    @app.get('/api/experiments')
    def experiments():
        return {'items': service.experiments()}

    @app.post('/api/experiments')
    async def export(request: Request):
        value = await body(request)
        start, end, slip, note = (value.get(k) for k in ('start', 'end', 'slippage_bps', 'note'))
        if not isinstance(start, str) or not isinstance(end, str) or not isinstance(note or '', str) or len(note or '') > 2000:
            raise PortalError('回测日期或备注格式无效。')
        expected = ({'expected_strategy_type': value['expected_strategy_type']}
                    if 'expected_strategy_type' in value else {})
        return await run_in_threadpool(service.export, start, end, slip, note or '', **expected)

    @app.get('/api/experiments/replay-series')
    def replay_series():
        source, items = service.replay_series()
        return {'source': source, 'count': len(items), 'dates': [item['date'] for item in items]}

    @app.post('/api/experiments/replay')
    async def replay_backtest(request: Request):
        value = await body(request)
        return await run_in_threadpool(service.replay_backtest, value)

    @app.get('/api/experiments/{ident}/code')
    def code(ident: str):
        folder = service._experiment(ident)
        from backtests.experiments import validate_export
        validate_export(folder)
        return FileResponse(folder / 'main.py', filename='main.py', media_type='text/plain')

    @app.post('/api/experiments/{ident}/result')
    async def import_result(ident: str, request: Request):
        return await run_in_threadpool(service.import_backtest, ident, await request.body())

    @app.post('/api/compare')
    async def compare(request: Request):
        value = await body(request)
        return await run_in_threadpool(service.compare, value.get('ids'))

    @app.post('/api/shutdown')
    def stop():
        with service.lock:
            service.idle()
            if shutdown is None:
                raise PortalError('当前运行方式不支持通过网页关闭服务。')
            service.closing = True
            shutdown()
        return {'message': '本地服务已停止。再次双击启动器即可打开。'}

    static = Path(root) / 'ui/dist'
    if static.is_dir():
        app.mount('/', StaticFiles(directory=static, html=True), name='portal')
    return app
