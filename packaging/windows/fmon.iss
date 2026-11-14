; Inno Setup script for fmon (https://jrsoftware.org/isinfo.php)
; Build the executables first (packaging\windows\build.ps1 does both steps), then:
;   ISCC packaging\windows\fmon.iss
; Produces dist\fmon-2.0.0-setup.exe

#define AppName "fmon"
#define AppVersion "2.0.0"
#define AppPublisher "Alejandro Castaneda"
#define DistDir "..\..\dist\fmon"

[Setup]
AppId={{6F1A0C1E-2B6A-4E7B-9C33-FMON00000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName={#AppName}
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest

[Tasks]
Name: "addtopath"; Description: "Add fmon to the PATH (use 'fmon' in any terminal)"; GroupDescription: "Command line:"
Name: "desktopicon"; Description: "Create a desktop shortcut for fmon GUI"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\fmon GUI"; Filename: "{app}\fmon-gui.exe"
Name: "{group}\fmon manual"; Filename: "{cmd}"; Parameters: "/k ""{app}\fmon.exe"" help"
Name: "{group}\Uninstall fmon"; Filename: "{uninstallexe}"
Name: "{autodesktop}\fmon GUI"; Filename: "{app}\fmon-gui.exe"; Tasks: desktopicon

[Registry]
; append the install folder to the user's PATH when the task is selected
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath('{app}')

[Run]
Filename: "{app}\fmon-gui.exe"; Description: "Launch fmon GUI"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(ExpandConstant(Param)) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
