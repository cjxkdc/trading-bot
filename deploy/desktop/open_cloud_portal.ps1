# Open the public HTTPS workbench. This launcher holds no credentials or processes.
$ErrorActionPreference = 'Stop'
try {
    $projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
    $settings = Get-Content -LiteralPath (Join-Path $projectDir 'configs\portal\cloud.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $url = [uri]$settings.url
    if ($url.Scheme -ne 'https' -or !$url.Host -or $url.UserInfo -or $url.Query -or $url.Fragment -or $url.AbsolutePath -ne '/') {
        throw '请在 configs/portal/cloud.json 配置工作台的 HTTPS 地址。'
    }
    Start-Process $url.AbsoluteUri
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Trading Bot · 云端工作台')
}
