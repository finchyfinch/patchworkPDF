; ============================================================
;  PatchworkPDF — Windows Installer Script
;  Requires Inno Setup 6  (https://jrsoftware.org/isinfo.php)
;
;  Build with one command:   packaging\win\build_installer.bat   (from repo root)
;  Or directly:              ISCC.exe packaging\win\installer.iss
;  Custom build dir:         ISCC.exe /DBuildDir="build\exe.win-amd64-3.12" packaging\win\installer.iss
; ============================================================

#define AppName        "PatchworkPDF"
#define AppPublisher   "Gareth Finch"
#define AppExeName     "PatchworkPDF.exe"
#define AppDescription "Patchwork PDF — a PDF page editor"

; AppVersion and AppVersionInfo are passed at compile-time by build_installer.bat:
;   /DAppVersion=1.34 /DAppVersionInfo=1.34.0.0
; They are read from version.json — do not hardcode them here.
#ifndef AppVersion
  #error AppVersion must be defined via /DAppVersion=X.XX on the ISCC command line (see build_installer.bat)
#endif
#ifndef AppVersionInfo
  #error AppVersionInfo must be defined via /DAppVersionInfo=X.XX.0.0 on the ISCC command line (see build_installer.bat)
#endif

; BuildDir is overridden at compile-time by build_installer.bat.
; The default covers a Python 3.12 / amd64 cx_Freeze build.
#ifndef BuildDir
  #define BuildDir "build\exe.win-amd64-3.12"
#endif

; ── Setup ────────────────────────────────────────────────────────────────────
[Setup]

; This script lives in packaging\win\ but all its relative paths (BuildDir,
; assets\, OutputDir, SetupIconFile, …) are written relative to the PROJECT
; ROOT. SourceDir re-bases the compiler two levels up so those paths resolve
; correctly no matter where ISCC is invoked from. {#SourcePath} is the dir of
; this .iss file (with trailing backslash).
SourceDir={#SourcePath}..\..

; IMPORTANT: AppId GUID must never change across versions —
; it is the key Inno Setup uses to detect an existing installation and
; perform a seamless in-place upgrade without requiring manual uninstall.
AppId={{C7E4A2F1-93D8-4B6E-A5C0-8D2F1E3B9A7C}

AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=
AppSupportURL=
AppUpdatesURL=
AppCopyright=Copyright (C) {#AppPublisher}

; Install location — defaults to C:\Program Files\PatchworkPDF
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes

; Output — installer exe lands in the Output\ subfolder
OutputDir=Output
OutputBaseFilename=PatchworkPDF_Setup_{#AppVersion}

; Branding
SetupIconFile=assets\icon.ico
WizardSmallImageFile=assets\icon_256.png
WizardStyle=modern

; Compression
Compression=lzma2/max
SolidCompression=yes

; Target: 64-bit Windows (matches the cx_Freeze amd64 build)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Version metadata stamped into the setup .exe itself
VersionInfoVersion={#AppVersionInfo}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} {#AppVersion} Setup
VersionInfoCompany={#AppPublisher}

; Add/Remove Programs entry
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}

; Upgrade behaviour:
;   - detect and close running instances of PatchworkPDF before overwriting
;   - always overwrite files (ignoreversion flag on [Files])
;   - no reboot required
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no

; File associations will change the shell cache
ChangesAssociations=yes

; Admin rights are needed for Program Files and HKCR file association.
; Allow a commandline /ALLUSERS or /CURRENTUSER override for advanced use.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline


; ── Languages ────────────────────────────────────────────────────────────────
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"


; ── Installer tasks (checkbox options shown during setup) ────────────────────
[Tasks]

; Desktop shortcut — opt-in
Name: "desktopicon"; \
  Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; \
  Flags: unchecked

; PDF file association — opt-in
Name: "fileassoc"; \
  Description: "Associate .pdf files with {#AppName}"; \
  GroupDescription: "File Associations:"; \
  Flags: unchecked


; ── Files ────────────────────────────────────────────────────────────────────
[Files]

; Copy the entire cx_Freeze build output tree into {app}.
; ignoreversion ensures every file is replaced on upgrade regardless of
; timestamps — important for frozen Python bytecode and bundled DLLs.
Source: "{#BuildDir}\*"; \
  DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; WebView2 Evergreen bootstrapper — the app renders through the Edge WebView2
; runtime (pywebview). Win11 ships it; some Win10 machines don't. Staged to
; {tmp} and run on-demand by the [Run] entry below (see WebView2Missing).
Source: "assets\MicrosoftEdgeWebview2Setup.exe"; \
  DestDir: "{tmp}"; \
  Flags: deleteafterinstall


; ── Shortcuts ────────────────────────────────────────────────────────────────
[Icons]

; Start Menu
Name: "{group}\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; \
  Comment: "{#AppDescription}"

Name: "{group}\Uninstall {#AppName}"; \
  Filename: "{uninstallexe}"

; Desktop (only when task is ticked)
Name: "{commondesktop}\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  WorkingDir: "{app}"; \
  Comment: "{#AppDescription}"; \
  Tasks: desktopicon


; ── Registry — PDF file association (only when task is ticked) ───────────────
[Registry]

; ---------- ProgId registration ----------

; The ProgId key itself
Root: HKCR; \
  Subkey: "PatchworkPDF.Document"; \
  ValueType: string; ValueName: ""; ValueData: "PDF Document"; \
  Flags: uninsdeletekey; \
  Tasks: fileassoc

; Icon shown in Explorer
Root: HKCR; \
  Subkey: "PatchworkPDF.Document\DefaultIcon"; \
  ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; \
  Tasks: fileassoc

; Open command — passes the file path to the executable
Root: HKCR; \
  Subkey: "PatchworkPDF.Document\shell\open\command"; \
  ValueType: string; ValueName: ""; \
  ValueData: """{app}\{#AppExeName}"" ""%1"""; \
  Tasks: fileassoc

; ---------- .pdf extension ----------

; Make PatchworkPDF the default handler for .pdf
Root: HKCR; \
  Subkey: ".pdf"; \
  ValueType: string; ValueName: ""; ValueData: "PatchworkPDF.Document"; \
  Flags: uninsdeletevalue; \
  Tasks: fileassoc

; Also register in OpenWithProgIds so the app appears in "Open With…"
Root: HKCR; \
  Subkey: ".pdf\OpenWithProgIds"; \
  ValueType: string; ValueName: "PatchworkPDF.Document"; ValueData: ""; \
  Flags: uninsdeletevalue; \
  Tasks: fileassoc

; ---------- Default Programs / Capabilities ----------

Root: HKLM; \
  Subkey: "SOFTWARE\{#AppPublisher}\{#AppName}\Capabilities"; \
  ValueType: string; ValueName: "ApplicationDescription"; \
  ValueData: "{#AppDescription}"; \
  Flags: uninsdeletekey; \
  Tasks: fileassoc

Root: HKLM; \
  Subkey: "SOFTWARE\{#AppPublisher}\{#AppName}\Capabilities\FileAssociations"; \
  ValueType: string; ValueName: ".pdf"; ValueData: "PatchworkPDF.Document"; \
  Tasks: fileassoc

; Register with Windows "Set Default Programs"
Root: HKLM; \
  Subkey: "SOFTWARE\RegisteredApplications"; \
  ValueType: string; ValueName: "{#AppName}"; \
  ValueData: "SOFTWARE\{#AppPublisher}\{#AppName}\Capabilities"; \
  Flags: uninsdeletevalue; \
  Tasks: fileassoc


; ── Post-install: offer to launch the app ────────────────────────────────────
[Run]
; Install the WebView2 runtime first, but only if it is missing (Check function).
; Runs silently and waits, so the app can launch into a ready runtime.
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; \
  Parameters: "/silent /install"; \
  StatusMsg: "Installing Microsoft WebView2 runtime..."; \
  Check: WebView2Missing; \
  Flags: waituntilterminated

Filename: "{app}\{#AppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent


; ── Pascal script — shell notification after file association change ──────────
[Code]

// Notifies the Windows shell to refresh its icon and association cache.
// Called via SHChangeNotify(SHCNE_ASSOCCHANGED=0x08000000, SHCNF_IDLIST=0, 0, 0).
procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal;
                         dwItem1: Longint; dwItem2: Longint);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('fileassoc') then
    SHChangeNotify($08000000, 0, 0, 0);
end;

// Detect whether the Evergreen WebView2 runtime is already installed.
// The runtime registers its version ('pv') under a fixed GUID, per-machine
// (HKLM, under WOW6432Node on 64-bit) or per-user (HKCU). A non-empty 'pv'
// that isn't "0.0.0.0" means the runtime is present.
function WebView2Missing: Boolean;
var
  pv: String;
begin
  Result := True;
  if RegQueryStringValue(HKLM,
       'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
       'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then
    Result := False
  else if RegQueryStringValue(HKCU,
       'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
       'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then
    Result := False;
end;
