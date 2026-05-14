; Inno Setup script for CopyTrades GUI.
; Build prerequisites:
;   1. pyinstaller CopyTrades.spec      (produces dist/CopyTrades/)
;   2. Install Inno Setup 6 (https://jrsoftware.org/isdl.php)
;   3. iscc installer.iss               (produces dist/CopyTrades-Setup-<ver>.exe)

#define MyAppName        "CopyTrades"
#define MyAppVersion     "0.1.0"
#define MyAppPublisher   "CopyTrades"
#define MyAppExeName     "CopyTrades.exe"

[Setup]
AppId={{6F22A4B9-3F4A-4B0C-9F8E-2A6D7C0B1C2D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=CopyTrades-Setup-{#MyAppVersion}
SetupIconFile=copytrades.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\CopyTrades\CopyTrades.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\CopyTrades\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
