[CmdletBinding()]
param([switch]$Preview)

$ErrorActionPreference = 'Stop'
try {
    if (-not $Preview) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            $child = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $PSCommandPath + '"')) -Verb RunAs -Wait -PassThru
            exit $child.ExitCode
        }
    }

    $wireless = @(Get-NetAdapter -Physical | Where-Object { $_.InterfaceType -eq 71 -or $_.NdisPhysicalMedium -eq 9 })
    if (-not $wireless.Count) { throw '找不到无线网卡。' }
    $campusProfile = Get-NetConnectionProfile -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^ChinaTelecom-EDU(5\.8G|2\.4G)( \d+)?$' -and $_.InterfaceIndex -in $wireless.ifIndex } |
        Select-Object -First 1
    $adapter = $wireless | Where-Object { $_.ifIndex -eq $campusProfile.InterfaceIndex } | Select-Object -First 1
    if (-not $adapter) { $adapter = $wireless | Where-Object Status -eq 'Up' | Select-Object -First 1 }
    if (-not $adapter) { $adapter = $wireless | Sort-Object @{ Expression = { $_.Name -ne 'WLAN' } }, ifIndex | Select-Object -First 1 }
    $interfaceId = ([guid]$adapter.InterfaceGuid).ToString('B')
    $interfaceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\' + $interfaceId
    if (-not (Test-Path -LiteralPath $interfaceKey)) { throw '找不到 WLAN 网络配置，未作修改。' }

    Write-Host '校园网 DNS 修复' -ForegroundColor Cyan
    Write-Host '将校园 Wi-Fi 的手动 DNS 恢复为自动获取，然后清除解析缓存。'
    foreach ($ssid in @('ChinaTelecom-EDU5.8G','ChinaTelecom-EDU2.4G')) {
        $encodedKey = ([Text.Encoding]::UTF8.GetBytes($ssid) | ForEach-Object {
            $hex = $_.ToString('X2')
            [string]$hex[1] + [string]$hex[0]
        }) -join ''
        $profileKey = Join-Path $interfaceKey $encodedKey
        if (Test-Path -LiteralPath $profileKey) {
            $old = (Get-ItemProperty -LiteralPath $profileKey).NameServer
            if ($Preview) {
                Write-Host ('[预览] ' + $ssid + '：当前手动 DNS = ' + $old)
            } else {
                Set-ItemProperty -LiteralPath $profileKey -Name NameServer -Value ''
                $remaining = (Get-ItemProperty -LiteralPath $profileKey).NameServer
                if ($remaining) { throw ('无法恢复 ' + $ssid + ' 的自动 DNS。') }
                Write-Host ($ssid + '：已恢复自动 DNS') -ForegroundColor Green
            }
        }
    }
    if ($Preview) {
        Write-Host ('[预览] 将重置 ' + $adapter.Name + ' 的 DNS 并清除缓存；本次没有修改任何设置。')
    } else {
        Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ResetServerAddresses
        Clear-DnsClientCache
        Write-Host 'DNS 已恢复自动获取，解析缓存已清除。请重新连接校园 Wi-Fi。' -ForegroundColor Green
        Write-Host '若认证页未弹出，可在浏览器打开 http://www.msftconnecttest.com/redirect'
        Read-Host '按回车关闭窗口'
    }
    exit 0
} catch {
    Write-Host ('修复未完成：' + $_.Exception.Message) -ForegroundColor Red
    if (-not $Preview) { Read-Host '按回车关闭窗口' }
    exit 1
}
