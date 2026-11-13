# Per-user installation of the portable fmon package (no administrator rights needed).
# Run from the unzipped folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Installs to %LOCALAPPDATA%\Programs\fmon, adds it to the user's PATH and creates
# Start Menu shortcuts. Remove everything again with uninstall.ps1 (also copied there).
$ErrorActionPreference = "Stop"
$source = $PSScriptRoot
$target = Join-Path $env:LOCALAPPDATA "Programs\fmon"
if (-not (Test-Path (Join-Path $source "fmon.exe"))) { throw "fmon.exe not found next to this script" }

Write-Host "Installing fmon to $target"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force

# user PATH
$path = [Environment]::GetEnvironmentVariable("Path", "User")
if (($path -split ";") -notcontains $target) {
    [Environment]::SetEnvironmentVariable("Path", ($path.TrimEnd(";") + ";" + $target), "User")
    Write-Host "Added to your PATH (open a new terminal to use 'fmon')"
}

# Start Menu shortcuts
$menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\fmon"
New-Item -ItemType Directory -Force -Path $menu | Out-Null
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $menu "fmon GUI.lnk"))
$lnk.TargetPath = Join-Path $target "fmon-gui.exe"; $lnk.WorkingDirectory = $target; $lnk.Save()
$lnk = $shell.CreateShortcut((Join-Path $menu "fmon manual.lnk"))
$lnk.TargetPath = "$env:ComSpec"; $lnk.Arguments = "/k `"$target\fmon.exe`" help"; $lnk.Save()
$lnk = $shell.CreateShortcut((Join-Path $menu "Uninstall fmon.lnk"))
$lnk.TargetPath = "powershell.exe"; $lnk.Arguments = "-ExecutionPolicy Bypass -File `"$target\uninstall.ps1`""; $lnk.Save()

Write-Host "Done. Start Menu > fmon > fmon GUI, or run 'fmon' in a new terminal."
