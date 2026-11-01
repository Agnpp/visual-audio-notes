param([switch]$SkipInstall, [ValidateSet('full','api')][string]$Flavor = 'full')
$ErrorActionPreference = 'Stop'
$env:VAN_BUILD_FLAVOR = $Flavor
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$DesktopPython = Join-Path $ProjectRoot '.venv-desktop\Scripts\python.exe'
$ResearchPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
function Invoke-CheckedNative {
    param([string]$Executable, [string[]]$Arguments)
    $PreviousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments 2>&1 | ForEach-Object { Write-Host $_.ToString() }
        $NativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($NativeExitCode -ne 0) { throw ('Native tool failed with exit code ' + $NativeExitCode) }
}
$WorkspacePrefix = $ProjectRoot.TrimEnd('\') + '\'
foreach ($RelativeTarget in @('build\desktop\pyinstaller', 'dist\VisualAudioNotes-0.3.1-windows-x64', 'dist\VisualAudioNotes-0.3.1-api-windows-x64')) {
    $AbsoluteTarget = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $RelativeTarget))
    if (-not $AbsoluteTarget.StartsWith($WorkspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build target must remain inside the project.'
    }
    $TargetPart = $AbsoluteTarget
    while ($TargetPart.Length -gt $ProjectRoot.Length) {
        if (Test-Path -LiteralPath $TargetPart) {
            $TargetInfo = Get-Item -LiteralPath $TargetPart -Force
            if (($TargetInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Build paths cannot be junctions or symbolic links.'
            }
        }
        $TargetPart = [System.IO.Path]::GetDirectoryName($TargetPart)
    }
}
Push-Location -LiteralPath $ProjectRoot
try {
    if (-not (Test-Path -LiteralPath $DesktopPython)) {
        if (Test-Path -LiteralPath $ResearchPython) {
            Invoke-CheckedNative -Executable 'uv' -Arguments @('venv', '--python', $ResearchPython, '.venv-desktop')
        } else {
            Invoke-CheckedNative -Executable 'uv' -Arguments @('venv', '--python', '3.12', '.venv-desktop')
        }
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create desktop environment.' }
    }
    if (-not $SkipInstall) {
        Invoke-CheckedNative -Executable 'uv' -Arguments @('pip', 'sync', '--python', $DesktopPython, 'requirements-desktop.lock')
        if ($LASTEXITCODE -ne 0) { throw 'Cannot install locked desktop dependencies.' }
    }
    & $DesktopPython 'tools\package_desktop.py' prepare
    if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare application icon.' }
    # No recursive deletion is performed. PyInstaller manages only these named
    # project-relative build/output locations, preserving user workspace data.
    & $DesktopPython 'tools\package_desktop.py' build
    if ($LASTEXITCODE -ne 0) { throw 'Desktop executable build failed.' }
    & $DesktopPython 'tools\package_desktop.py' package
    if ($LASTEXITCODE -ne 0) { throw 'Desktop archive packaging failed.' }
} finally {
    Pop-Location
}
