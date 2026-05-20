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
; Top-level copy of the application icon. PyInstaller embeds it
; under {app}\_internal\copytrades.ico when bundled as a data file,
; but the [Icons] entries below (and Windows shortcut creation in
; general) reference {app}\copytrades.ico. Without this top-level
; copy the desktop / Start-menu shortcut points at a non-existent
; IconFilename, Windows shows a blank icon, and the embedded-icon
; fallback never fires because the IconFilename is explicit.
Source: "{#MyIconFile}"; DestDir: "{app}"; Flags: ignoreversion
; Standalone uninstall-time helper. Kept outside the PyInstaller
; bundle so its path is stable across PyInstaller layout changes;
; the [UninstallRun] entry below relies on a fixed {app}-relative
; path. uninstallneveruninstall keeps it on disk until uninstall
; actually fires (Inno deletes installed files before running
; [UninstallRun], so without this flag the script would be gone by
; the time we try to invoke it).
Source: "tools\uninstall_services.cmd"; DestDir: "{app}"; \
    Flags: ignoreversion uninsneveruninstall

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
; of locked DLLs until reboot.
;
; Three things the previous one-liner got wrong (and why this entry is
; now an external script + shellexec/runas):
;   1. PATH: the bundled nssm.exe lives under {app}\_internal\... after
;      PyInstaller, not {app}\src\..., so the old `nssm remove` invocation
;      pointed at a non-existent path and silently no-op'd under runhidden.
;   2. ELEVATION: PrivilegesRequired=lowest means the uninstaller runs as
;      the user. Modifying the SCM (stop/delete a service) requires admin,
;      so the old `nssm remove` calls would have been denied by the SCM
;      even if the path were correct. shellexec + Verb: runas now prompts
;      for UAC at uninstall time.
;   3. STOP-THEN-DELETE: deleting a RUNNING service marks it for deletion
;      but leaves it active until the next reboot, defeating the purpose.
;      The helper script stops first, waits, then deletes.
;
; The script uses sc.exe (always present in Windows) instead of the
; bundled nssm.exe so it can't break if PyInstaller's layout shifts.
Filename: "{app}\uninstall_services.cmd"; \
    Flags: shellexec waituntilterminated; \
    Verb: "runas"; \
    StatusMsg: "Removing CopyTrades Windows services..."; \
    RunOnceId: "RemoveCopyTradesServices"

[UninstallDelete]
; Wipe the install dir on uninstall — PyInstaller scatters .pyc + cache
; files that don't get tracked by [Files].
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\src"
; uninstall_services.cmd is flagged uninsneveruninstall so it survives
; until [UninstallRun] has used it; clean it up afterwards.
Type: files; Name: "{app}\uninstall_services.cmd"

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
