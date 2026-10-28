[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ShortcutDirectory = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$projectDirectory = (Resolve-Path -LiteralPath $ProjectRoot).Path
$shortcutDirectoryPath = (Resolve-Path -LiteralPath $ShortcutDirectory).Path
$application = Join-Path $projectDirectory 'ClassroomAttendance.exe'
$entry = Join-Path $projectDirectory 'app_entry.py'
$desktopEntry = Join-Path $projectDirectory 'tools\desktop_entry.pyw'
$sourceMode = -not (Test-Path -LiteralPath $application -PathType Leaf)
if ($sourceMode) {
    $application = Join-Path $projectDirectory '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
        throw 'Python environment missing. Create .venv with Python 3.11 first; see README.md.'
    }
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf) -or -not (Test-Path -LiteralPath $desktopEntry -PathType Leaf)) {
        throw 'app_entry.py or tools/desktop_entry.pyw is missing from the selected project directory.'
    }
    $runtimeConsole = Join-Path $projectDirectory '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $runtimeConsole -PathType Leaf)) {
        throw 'The project Python interpreter is missing.'
    }
    $runtimeEntry = & $runtimeConsole -c 'from pathlib import Path; import sys; print(Path(sys._base_executable).with_name("pythonw.exe"))'
    if ($LASTEXITCODE -ne 0 -or -not $runtimeEntry) {
        throw 'Cannot locate the installed Python used by .venv.'
    }
    $application = ($runtimeEntry | Select-Object -Last 1).Trim()
    if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
        throw 'The installed Python GUI interpreter is missing.'
    }
}

$entries = @(
    @{ Name = '课堂签到—启动入口'; Role = 'launcher' },
    @{ Name = '课堂签到—教师管理'; Role = 'admin' },
    @{ Name = '课堂签到—学生签到'; Role = 'student' }
)
$shell = New-Object -ComObject WScript.Shell
try {
    foreach ($item in $entries) {
        $path = Join-Path $shortcutDirectoryPath ($item.Name + '.lnk')
        $shortcut = $shell.CreateShortcut($path)
        try {
            $shortcut.TargetPath = $application
            $shortcut.Arguments = if ($sourceMode) {
                '"' + $desktopEntry + '" --role ' + $item.Role
            } else {
                '--role ' + $item.Role
            }
            $shortcut.WorkingDirectory = $projectDirectory
            $shortcut.IconLocation = $application + ',0'
            $shortcut.Description = 'Classroom Attendance / ' + $item.Role + ' / tools/create_shortcuts.ps1'
            $shortcut.WindowStyle = 1
            $shortcut.Save()
        } finally {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut)
        }
        Write-Output $path
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}
