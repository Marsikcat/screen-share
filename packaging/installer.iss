; Installer for MarinCall (Inno Setup 6). Built by tools\build.py:
;   ISCC /DAppVersion=X.Y.Z packaging\installer.iss
; Per-user install (no admin rights), like Discord: %LOCALAPPDATA%\Programs\MarinCall.
; The in-app updater runs this silently (/VERYSILENT /CLOSEAPPLICATIONS); it then starts the app again.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "MarinCall"
#define AppExe "MarinCall.exe"

[Setup]
AppId={{40056B9A-7955-48A4-A00A-65EF5F000482}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Marsikcat
AppPublisherURL=https://github.com/Marsikcat/screen-share
AppSupportURL=https://github.com/Marsikcat/screen-share/issues
AppUpdatesURL=https://github.com/Marsikcat/screen-share/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} — установщик
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=MarinCall-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Запускать вместе с Windows"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\MarinCall\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; files of an older version that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"
; the app was called МойДискорд until 3.2 — drop its exe and its shortcuts
Type: files; Name: "{app}\MoyDiscord.exe"
Type: files; Name: "{autoprograms}\МойДискорд.lnk"
Type: files; Name: "{autodesktop}\МойДискорд.lnk"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\ScreenShare — классическая демонстрация"; Filename: "{app}\ScreenShare.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; the same value the app writes from Settings → Startup
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --autostart"; Tasks: autostart
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; silent run = in-app update: start the new version right away
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im ScreenShare.exe"; Flags: runhidden; RunOnceId: "StopClassic"
