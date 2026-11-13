# Builds the Windows executables and an installer.
#   .\packaging\windows\build.ps1               PyInstaller (recommended) + installer
#   .\packaging\windows\build.ps1 -Py2exe       build with py2exe instead
#   .\packaging\windows\build.ps1 -NoInstaller  executables only
#   .\packaging\windows\build.ps1 -Portable     skip Inno Setup, make the portable ZIP installer
#
# Installer options:
#   * Inno Setup 6 (free, https://jrsoftware.org/isdl.php or: winget install JRSoftware.InnoSetup)
#     -> dist\fmon-2.0.0-setup.exe, a classic setup wizard.
#   * Without Inno Setup the script produces dist\fmon-2.0.0-windows-portable.zip, which
#     contains the program plus install.ps1 / uninstall.ps1 (per-user install, no admin rights).
param([switch]$Py2exe, [switch]$NoInstaller, [switch]$Portable)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$version = (python -c "import sys; sys.path.insert(0, 'src'); import fmon; print(fmon.__version__)")

python -m pip install --upgrade pip | Out-Null
python -m pip install -e . | Out-Null
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

if ($Py2exe) {
    python -m pip install py2exe | Out-Null
    python packaging\windows\setup_py2exe.py py2exe
} else {
    python -m pip install pyinstaller | Out-Null
    python -m PyInstaller --noconfirm --clean packaging\windows\fmon.spec
}

Write-Host "Smoke test:"
& dist\fmon\fmon.exe --version
Copy-Item packaging\windows\install.ps1, packaging\windows\uninstall.ps1 dist\fmon\

if ($NoInstaller) { Write-Host "Done. Executables in dist\fmon\"; exit 0 }

function Find-Iscc {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"     # per-user installation
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$iscc = $null
if (-not $Portable) {
    $iscc = Find-Iscc
    if (-not $iscc -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $answer = Read-Host "Inno Setup 6 is not installed. Install it now with winget? [Y/n]"
        if ($answer -eq "" -or $answer -match "^[Yy]") {
            winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements
            $iscc = Find-Iscc
        }
    }
}

if ($iscc) {
    & $iscc packaging\windows\fmon.iss
    Write-Host "Installer: dist\fmon-$version-setup.exe"
} else {
    Write-Host "Inno Setup not available - building the portable ZIP installer instead."
    $zip = "dist\fmon-$version-windows-portable.zip"
    Compress-Archive -Path dist\fmon\* -DestinationPath $zip -Force
    Write-Host "Portable package: $zip"
    Write-Host "  unzip it anywhere and run install.ps1 (per-user install: Start Menu, PATH, uninstaller)"
    Write-Host "  or just run fmon-gui.exe / fmon.exe from the folder."
}
Write-Host "Done. Executables in dist\fmon\"
