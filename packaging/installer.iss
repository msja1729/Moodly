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

[UninstallRun]
; Stop a running copy so its files can be removed.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM AmbientMood.exe"; Flags: runhidden; RunOnceId: "StopApp"
