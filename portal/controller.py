"""Always-available local Portal and explicit trading-backend power controls."""
from pathlib import Path
import json
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.control_contracts import ControlError
from app.execution_policy import CURRENT_EXECUTION_POLICY
from portal.app import LocalOnly, body
from portal.runtime import BackendRuntime
from portal.access import COOKIE, OwnerSessions, RemoteOnly


def create_controller(root, *, port, instance, shutdown=None, runtime=None, access=None):
    runtime = runtime or BackendRuntime(root)
    token = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.portal_token = token
    owner = OwnerSessions(access) if access is not None else None
    server_status = None
    if owner is not None:
        from portal.server_status import ServerStatus
        server_status = ServerStatus(root, runtime)
    app.state.server_status = server_status
    if owner is None:
        app.add_middleware(LocalOnly, port=port, token=token)
    else:
        app.add_middleware(RemoteOnly, access=owner, token=token)

    @app.exception_handler(ControlError)
    async def control_error(request, exc):
        return JSONResponse({'message': str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({'message': '后台控制操作失败，请检查状态后再操作。'}, status_code=500)

    @app.get('/health')
    async def health():
        return {'application': 'trading-bot-portal', 'instance': instance,
                'execution_policy': CURRENT_EXECUTION_POLICY.identifier,
                'execution_scope': CURRENT_EXECUTION_POLICY.public(), 'runtime_controls': True}

    @app.get('/api/session')
    def session():
        return {'token': token}

    @app.get('/api/auth/status')
    def authentication(request: Request):
        return {'enabled': owner is not None, 'deployment': 'server' if owner else 'desktop',
                'authenticated': owner is None or owner.authenticated(request.cookies.get(COOKIE)),
                'login_token': token if owner else None}

    @app.post('/api/auth/login')
    async def login(request: Request):
        if owner is None:
            raise ControlError('本地工作台不使用服务器登录。')
        value = await body(request)
        if set(value) != {'username', 'password'}:
            raise ControlError('请填写工作台用户名与密码。')
        cookie = await run_in_threadpool(owner.login, value['username'], value['password'])
        # Cookies do not travel through the worker proxy. Neither passwords nor
        # session credentials are forwarded to a broker or written to job logs.
        response = JSONResponse({'message': '已登录服务器工作台。'})
        response.set_cookie(COOKIE, cookie, httponly=True, secure=owner.secure,
                            samesite='strict', max_age=owner.settings.session_seconds, path='/')
        return response

    @app.post('/api/auth/logout')
    async def logout(request: Request):
        if await body(request):
            raise ControlError('退出登录不接受交易参数。')
        if owner:
            owner.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'message': '已退出登录；后台运行状态保持不变。'})
        response.delete_cookie(COOKIE, path='/', secure=owner.secure if owner else False,
                               httponly=True, samesite='strict')
        return response

    @app.get('/api/runtime')
    def status():
        return runtime.status()

    @app.get('/api/server/status')
    async def server_health():
        if server_status is None:
            return JSONResponse({'message': '本地工作台没有服务器管理页面。'}, status_code=404)
        return await run_in_threadpool(server_status.status)

    @app.get('/api/server/diagnostics')
    async def server_diagnostics():
        if server_status is None:
            return JSONResponse({'message': '本地工作台没有服务器管理页面。'}, status_code=404)
        from deploy.server.diagnose import inspect
        report = await run_in_threadpool(inspect, root)
        return JSONResponse(report, headers={
            'Content-Disposition': 'attachment; filename="trading-bot-diagnostics.json"'})

    @app.post('/api/runtime/start')
    async def start(request: Request):
        if await body(request):
            raise ControlError('启动后台不接受交易参数。')
        result = await run_in_threadpool(runtime.start)
        if server_status is not None:
            server_status.invalidate()
        return result

    @app.post('/api/runtime/stop')
    async def stop(request: Request):
        if await body(request):
            raise ControlError('停止后台不接受额外参数。')
        result = await run_in_threadpool(runtime.stop)
        if server_status is not None:
            server_status.invalidate()
        return result

    @app.post('/api/shutdown')
    async def exit_portal(request: Request):
        if await body(request):
            raise ControlError('退出操作不接受额外参数。')
        if owner is not None:
            raise ControlError('服务器工作台保持在线；请使用后台开关或退出登录。')
        if shutdown is None:
            raise ControlError('当前运行方式不支持退出工作台。')
        await run_in_threadpool(runtime.stop, persist=False)
        with runtime.lock:
            runtime.closing = True
        shutdown()
        return {'message': '工作台正在退出。再次双击 Trading Bot.exe 即可打开。'}

    @app.api_route('/api/{path:path}', methods=['GET', 'POST'])
    async def proxy(path: str, request: Request):
        if path.startswith(('runtime/', 'session/', 'shutdown/', 'auth/', 'server/')):
            return JSONResponse({'message': '未知控制接口。'}, status_code=404)
        query = '?' + request.url.query if request.url.query else ''
        content = await request.body() if request.method == 'POST' else None
        if path == 'broker-auth/start' and request.method == 'POST':
            if await body(request):
                raise ControlError('网页授权不接受自定义回调地址。')
            origin = access.origin if access else f'http://127.0.0.1:{port}'
            content = json.dumps({'callback_origin': origin}).encode()
        reply = await run_in_threadpool(runtime.forward, request.method, '/api/' + path + query,
                                       content, request.headers.get('content-type'))
        return Response(reply.body, status_code=reply.status, headers=reply.headers)

    static = Path(root) / 'ui/dist'
    if static.is_dir():
        app.mount('/', StaticFiles(directory=static, html=True), name='portal')
    return app
