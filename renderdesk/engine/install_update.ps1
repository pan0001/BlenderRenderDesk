param([Parameter(Mandatory=$true)][string]$Plan)
$ErrorActionPreference = 'Stop'
$planData = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$movedOld = $false
$movedNew = $false
$launched = $null
function Save-Result([string]$status, [string]$message) {
    $resultData = @{status=$status; message=$message; version=$planData.version; backup=$planData.backup; time=[DateTime]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText($planData.result, ($resultData | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
}
try {
    # Paths must remain siblings. No recursive deletion and no task-data operations.
    $installPath = [IO.Path]::GetFullPath($planData.install)
    $backupPath = [IO.Path]::GetFullPath($planData.backup)
    $stageRoot = [IO.Path]::GetFullPath($planData.stage_root)
    $stagedPath = [IO.Path]::GetFullPath($planData.staged)
    $parentPath = [IO.Path]::GetDirectoryName($installPath)
    if ([IO.Path]::GetDirectoryName($backupPath) -ne $parentPath -or [IO.Path]::GetDirectoryName($stageRoot) -ne $parentPath -or [IO.Path]::GetDirectoryName($stagedPath) -ne $stageRoot -or !(Split-Path $stageRoot -Leaf).StartsWith('.renderdesk-update-')) { throw 'Invalid update directories' }
    if (Test-Path -LiteralPath $backupPath) { throw 'Backup directory already exists' }
    $oldProcess = Get-Process -Id $planData.pid -ErrorAction SilentlyContinue
    if ($oldProcess) {
        $birth = ($oldProcess.StartTime.ToUniversalTime() - [DateTime]'1970-01-01').TotalSeconds
        if ([Math]::Abs($birth - $planData.birth) -gt 2) { throw 'Manager process identity changed' }
        if (!$oldProcess.WaitForExit(180000)) { throw 'Manager did not exit; update was not installed' }
    }
    # Retry transient antivirus / loader locks. Never terminate rendering processes.
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try { Move-Item -LiteralPath $installPath -Destination $backupPath; $movedOld=$true; break }
        catch { if ($attempt -eq 29) { throw }; Start-Sleep -Milliseconds 500 }
    }
    Move-Item -LiteralPath $stagedPath -Destination $installPath
    $movedNew = $true
    Save-Result 'starting' '新版已安装，正在验证启动。'
    $launched = Start-Process -FilePath (Join-Path $installPath 'BlenderRenderDesk.exe') -ArgumentList $planData.updated_arguments -WorkingDirectory $installPath -WindowStyle Hidden -PassThru
    for ($attempt = 0; $attempt -lt 180; $attempt++) {
        if (Test-Path -LiteralPath $planData.ready) {
            $readyData = Get-Content -LiteralPath $planData.ready -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($readyData.version -eq $planData.version) {
                Save-Result 'installed' '更新成功。旧版本备份保留在程序目录旁。'
                exit 0
            }
        }
        if ($launched.HasExited) { throw 'New manager exited before it became ready' }
        Start-Sleep -Milliseconds 500
    }
    throw 'New manager did not become ready in time'
} catch {
    $failure = $_.Exception.Message
    try {
        if ($launched -and !$launched.HasExited) {
            # This handle is only the manager we just launched, never Blender or its children.
            $launched.Kill()
            [void]$launched.WaitForExit(15000)
        }
        if ($movedNew) { Move-Item -LiteralPath $installPath -Destination $stagedPath }
        if ($movedOld) { Move-Item -LiteralPath $backupPath -Destination $installPath }
        Save-Result 'failed' ('更新失败，旧版本已保留：' + $failure)
        if (!$oldProcess -or $oldProcess.HasExited) {
            Start-Process -FilePath (Join-Path $installPath 'BlenderRenderDesk.exe') -ArgumentList $planData.arguments -WorkingDirectory $installPath -WindowStyle Hidden
        }
    } catch {
        Save-Result 'failed' ('更新恢复失败，请从备份目录启动旧版本：' + $planData.backup + '；' + $_.Exception.Message)
    }
    exit 1
}
