param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [string]$ConfigPath = '.\config.local.json'
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw '仅支持Windows' }
$syncPython = (Resolve-Path -LiteralPath $PythonPath).Path
$syncConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
if ($syncPython.Contains('"') -or $syncConfig.Contains('"')) { throw '路径不能包含双引号' }
$syncSettings = Get-Content -LiteralPath $syncConfig -Raw -Encoding UTF8 | ConvertFrom-Json
$syncTimes = @($syncSettings.schedule)
if ($syncTimes.Count -eq 0 -or (@($syncTimes | Select-Object -Unique).Count -ne $syncTimes.Count)) { throw '计划时间缺失或重复' }
foreach ($syncTime in $syncTimes) {
    if ($syncTime -notmatch '^(?:[01]\d|2[0-3]):[0-5]\d$') { throw '时间必须为HH:MM' }
}
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw '请先确认Windows为北京时间；脚本不自动修改系统时区' }
$syncTaskName = 'BuyinOrderSync'
if (Get-ScheduledTask -TaskName $syncTaskName -ErrorAction SilentlyContinue) { throw '同名任务已存在，停止；请先人工核对，不覆盖现有任务' }
$syncArgs = '-m buyin_order_sync --config "' + $syncConfig + '" run --sync --origin scheduled'
$syncAction = New-ScheduledTaskAction -Execute $syncPython -Argument $syncArgs -WorkingDirectory (Split-Path -Parent $syncConfig)
$syncTriggers = @($syncTimes | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
$syncUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$syncPrincipal = New-ScheduledTaskPrincipal -UserId $syncUser -LogonType Interactive -RunLevel Limited
$syncPolicy = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $syncTaskName -Action $syncAction -Trigger $syncTriggers -Principal $syncPrincipal -Settings $syncPolicy | Out-Null
$syncReadback = Get-ScheduledTask -TaskName $syncTaskName
$syncActual = @($syncReadback.Triggers | ForEach-Object { $_.StartBoundary.Substring(11,5) } | Sort-Object)
if (($syncActual -join ',') -ne (($syncTimes | Sort-Object) -join ',')) { throw '读回时间不一致，请人工检查任务' }
Write-Output ('任务已创建并读回：' + $syncTaskName + ' ' + ($syncActual -join ', ') + '。没有立即启动同步。')
