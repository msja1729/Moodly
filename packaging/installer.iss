; Inno Setup script: wraps dist/AmbientMood/ into dist/AmbientMood-Setup-<version>.exe.
; Built by packaging/build.ps1, which passes the version (/DAppVersion=1.0.0).
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E1D2B7A-4C1F-4B8E-9A55-3F2C8D0A7B11}
AppName=Ambient Mood
AppVersion={#AppVersion}
AppPublisher=msja1729
DefaultDirName={autopf}\Ambient Mood
DefaultGroupName=Ambient Mood
OutputDir=..\dist
OutputBaseFilename=AmbientMood-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user by default (no admin prompt; the app keeps its data per user anyway).
; The dialog lets someone choose "install for all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\AmbientMood.exe
; The app keeps settings and data in %APPDATA%\AmbientMood; the uninstaller leaves them.

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start Ambient Mood when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\AmbientMood\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Ambient Mood"; Filename: "{app}\AmbientMood.exe"
Name: "{group}\Uninstall Ambient Mood"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Ambient Mood"; Filename: "{app}\AmbientMood.exe"; Tasks: desktopicon
Name: "{userstartup}\Ambient Mood"; Filename: "{app}\AmbientMood.exe"; Tasks: startup

[Run]
Filename: "{app}\AmbientMood.exe"; Description: "Start Ambient Mood"; Flags: nowait postinstall skipifsilent

[Code]
// A running copy is asked to quit through a file it watches (%APPDATA%\AmbientMood\data\app.quit),
// so it hands the songs back to Spotify's queue, deletes the session playlist and restores the
// screen colour. Only if it hasn't exited after QuitWaitSeconds is it killed.
const
  QuitWaitSeconds = 40;

function AppRunning(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq AmbientMood.exe" /NH | find /I "AmbientMood.exe" >NUL',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure CloseRunningApp();
var
  QuitFile: String;
  I, Code: Integer;
begin
  if not AppRunning() then
    Exit;
  QuitFile := ExpandConstant('{userappdata}\AmbientMood\data\app.quit');
  ForceDirectories(ExtractFileDir(QuitFile));
  SaveStringToFile(QuitFile, '', False);
  for I := 1 to QuitWaitSeconds do
  begin
    if not AppRunning() then
      Break;
    Sleep(1000);
  end;
  if AppRunning() then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AmbientMood.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  DeleteFile(QuitFile);
end;

// Installing over a running copy (an upgrade).
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp();
  Result := '';
end;

// Uninstalling: after the user confirmed, before any file is removed.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    CloseRunningApp();
end;
