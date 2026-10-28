#requires -Version 5.1
param([string]$Python = '.venv\Scripts\python.exe')
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$buildProject = [System.IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$buildOutput = Join-Path $buildProject 'dist'
$buildWork = Join-Path $buildProject 'build'
$buildTools = Join-Path $buildProject '.build-tools'
$buildPipCache = Join-Path $buildWork 'pip-cache'
foreach ($buildTarget in @($buildOutput, $buildWork, $buildTools)) {
    if (-not [System.IO.Path]::GetFullPath($buildTarget).StartsWith($buildProject + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw '构建输出必须位于项目目录内。'
    }
}
if (-not [System.IO.Path]::IsPathRooted($Python)) { $Python = Join-Path $buildProject $Python }
& $Python -c "import sys; assert sys.version_info[:2] == (3, 11), 'Python 3.11 required'"
if ($LASTEXITCODE -ne 0) { throw '构建需要 Python 3.11。' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw '运行依赖检查失败。' }
New-Item -ItemType Directory -Path $buildPipCache -Force | Out-Null
& $Python -m pip install --disable-pip-version-check --upgrade --cache-dir $buildPipCache --target $buildTools -r (Join-Path $buildProject 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw '构建依赖安装失败。' }
$buildOldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $buildTools
    & $Python -m PyInstaller --noconfirm --clean --distpath $buildOutput --workpath $buildWork (Join-Path $buildProject 'ClassroomAttendance.spec')
    if ($LASTEXITCODE -ne 0) { throw '发行包构建失败。' }
} finally { $env:PYTHONPATH = $buildOldPythonPath }
$buildBundle = Join-Path $buildOutput 'ClassroomAttendance'
$buildReport = Join-Path $buildWork 'package-self-test.json'
$buildTestData = Join-Path $env:TEMP ('classroom-package-smoke-' + [guid]::NewGuid().ToString('N'))
$buildExe = Join-Path $buildBundle 'ClassroomAttendance.exe'
[System.IO.File]::Delete($buildReport)
$buildStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
$buildStartInfo.FileName = $buildExe
$buildStartInfo.Arguments = '--self-test "' + $buildReport + '" --data-dir "' + $buildTestData + '"'
$buildStartInfo.WorkingDirectory = $buildBundle
$buildStartInfo.UseShellExecute = $false
$buildStartInfo.CreateNoWindow = $true
$buildProcess = [System.Diagnostics.Process]::new()
try {
    $buildProcess.StartInfo = $buildStartInfo
    if (-not $buildProcess.Start()) { throw '无法启动发行包自检。' }
    if (-not $buildProcess.WaitForExit(180000)) {
        $buildProcess.Kill()
        $buildProcess.WaitForExit()
        throw '发行包自检超过 180 秒，已结束本次自检进程。'
    }
    $buildSelfTestExit = $buildProcess.ExitCode
} finally { $buildProcess.Dispose() }
if ($buildSelfTestExit -ne 0 -or -not (Test-Path -LiteralPath $buildReport) -or -not (Get-Content -LiteralPath $buildReport -Raw -Encoding UTF8 | ConvertFrom-Json).ok) {
    throw '发行包自检失败，请检查 build/package-self-test.json。'
}
Copy-Item -LiteralPath (Join-Path $buildProject 'LICENSE') -Destination $buildBundle
Copy-Item -LiteralPath (Join-Path $buildProject 'THIRD_PARTY_NOTICES.md') -Destination $buildBundle
if (Get-ChildItem -LiteralPath $buildBundle -Recurse -File -Filter '*.bat') {
    throw '发行目录包含旧 BAT 文件，请清理旧输出后重新构建。'
}
Copy-Item -LiteralPath (Join-Path $buildProject 'docs/deployment.md') -Destination (Join-Path $buildBundle '使用说明.md')
Copy-Item -LiteralPath (Join-Path $buildProject 'docs/maintenance.md') -Destination (Join-Path $buildBundle '现场维护说明.md')
Copy-Item -LiteralPath (Join-Path $buildProject 'docs/face-lifecycle.md') -Destination (Join-Path $buildBundle '人脸授权与清理说明.md')
$buildZip = Join-Path $buildOutput 'ClassroomAttendance-0.2.0-preview10-win-x64.zip'
Compress-Archive -LiteralPath $buildBundle -DestinationPath $buildZip -Force
$buildHash = (Get-FileHash -LiteralPath $buildZip -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($buildZip + '.sha256', $buildHash + '  ' + [System.IO.Path]::GetFileName($buildZip) + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Output ('发行包：' + $buildZip)
Write-Output ('SHA-256：' + $buildHash)
