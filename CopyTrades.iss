; CopyTrades — Inno Setup installer script.
;
; Compiled by Inno Setup (iscc.exe). Takes the PyInstaller bundle at
; dist\CopyTrades\ and wraps it into a single self-extracting installer.
;
; Usage:
;     1. Run pyinstaller first:    .venv\Scripts\python -m PyInstaller --clean --noconfirm CopyTrades.spec
;     2. Compile the installer:    iscc CopyTrades.iss
;     3. Distribute:               installer\CopyTradesSetup-<version>.exe
;
; The end user double-clicks the installer .exe and gets a normal
; Windows install wizard. No Python required on the target machine.

#define MyAppName       "CopyTrades"
#define MyAppVersion    "0.1.0"
#define MyAppPublisher  "CopyTrades"
#define MyAppExeName    "CopyTrades.exe"
#define MyAppDirName    "CopyTrades"
#define MyBundleDir     "dist\CopyTrades"
#define MyIconFile      "copytrades.ico"

[Setup]
; AppId is the immutable identifier Windows uses to recognise upgrades.
; Generate once, never change — replacing it would make older installs
; orphans in Add/Remove Programs. Created with `iscc /?` / Tools menu.
AppId={{9F2D5A1F-7B6C-4A1E-9D62-CAFEC0FFEE12}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/
AppSupportURL=https://github.com/
AppUpdatesURL=https://github.com/
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}

; %LOCALAPPDATA%\CopyTrades — per-user install. No admin needed, no UAC
; prompt during install. The app needs admin only when registering
; Windows services, which is gated by its own UAC prompt later.
DefaultDirName={localappdata}\{#MyAppDirName}
DisableDirPage=auto
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

OutputDir=installer
OutputBaseFilename=CopyTradesSetup-{#MyAppVersion}
SetupIconFile={#MyIconFile}

; LZMA2/ultra gets the best compression for the 200 MB PyInstaller
; bundle — typically ~30% savings vs the raw folder.
Compression=lzma2/ultra
SolidCompression=yes

; Modern installer chrome.
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}

; ARM64 not supported (PyInstaller bundle is x86_64 Python).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Pull the entire PyInstaller bundle. recursesubdirs picks up the
; _internal/ folder with PySide6, qfluentwidgets, telethon, etc.
; createallsubdirs ensures empty dirs (rare) survive too.
Source: "{#MyBundleDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; IconFilename: "{app}\{#MyIconFile}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; IconFilename: "{app}\{#MyIconFile}"; \
    Tasks: desktopicon

[Run]
; Offer to launch the app at the end of the wizard.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop + delete any CopyTrades services (CT-*) before wiping the install
; dir, otherwise %LOCALAPPDATA%\CopyTrades survives as a directory full
; of locked DLLs until reboot. The bundled nssm.exe does both in one
; command. The shell loop enumerates current services; if none exist,
; the loop body just doesn't execute — no error, no popup.
Filename: "{cmd}"; Parameters: "/C for /f ""tokens=2"" %S in ('sc.exe query state^= all ^| findstr /b ""SERVICE_NAME: CT-""') do ""{app}\src\gui\resources\nssm.exe"" remove %S confirm"; \
    Flags: runhidden; RunOnceId: "RemoveCopyTradesServices"

[UninstallDelete]
; Wipe the install dir on uninstall — PyInstaller scatters .pyc + cache
; files that don't get tracked by [Files].
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\src"

[Code]
{ Refuse to install if PyInstaller bundle is missing — typical cause is
  the operator ran iscc before pyinstaller. Surfaces a clear error
  instead of producing a broken installer. }
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption :=
    WizardForm.WelcomeLabel2.Caption + #13#10 + #13#10 +
    'This will install CopyTrades into your local AppData so no admin ' +
    'rights are required to install or update. Admin rights are still ' +
    'needed later to register the Telegram listener, bot, and API as ' +
    'Windows services, but the app prompts for that on first run.';
end;
