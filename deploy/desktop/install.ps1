$ErrorActionPreference = 'Stop'
$projectDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Push-Location -LiteralPath $projectDir
try {
    if (Test-Path -LiteralPath 'runs\portal\server.json') {
        throw '请先从工作台完整退出，再安装依赖。'
    }
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '请先安装 Python 3.12，再重试。' }
    & '.venv\Scripts\python.exe' -m pip install -r requirements/server.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
    Write-Host '基础面板与网页授权依赖已安装。OpenD SDK 请按实际券商安装；账户数据需要重新配置。'
} finally { Pop-Location }
