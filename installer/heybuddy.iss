; HeyBuddy installer (Inno Setup 6). Built in CI:
;   iscc /DAppVersion=0.4.0 /DSourceExe=..\dist\HeyBuddy.exe installer\heybuddy.iss
;
; * Per-user install (no admin prompt) into %LOCALAPPDATA%\Programs\HeyBuddy,
;   like HeyClicky's Velopack install under %LOCALAPPDATA%.
; * Start menu shortcut, optional desktop shortcut, optional "start with
;   Windows" (HKCU Run key, same value the in-app Settings toggle writes).
; * Uninstall removes the program; settings, memory and jobs in
;   %APPDATA%\HeyBuddy are kept unless the user ticks the box.

#ifndef AppVersion
  #define AppVersion "0.4.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\dist\HeyBuddy.exe"
#endif

[Setup]
AppId={{6F1B7C8E-3D2A-4C55-9B61-2A9E4F0D7B11}
AppName=HeyBuddy
AppVersion={#AppVersion}
AppVerName=HeyBuddy {#AppVersion}
AppPublisher=HeyBuddy (Hem456anth)
AppPublisherURL=https://github.com/Hem456anth/HeyBuddy
AppSupportURL=https://github.com/Hem456anth/HeyBuddy/issues
DefaultDirName={localappdata}\Programs\HeyBuddy
DefaultGroupName=HeyBuddy
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=HeyBuddy-Setup-{#AppVersion}
SetupIconFile=..\assets\heybuddy.ico
UninstallDisplayIcon={app}\HeyBuddy.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start HeyBuddy when I sign in to Windows"; GroupDescription: "Startup:"

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "..\SECURITY.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\HeyBuddy"; Filename: "{app}\HeyBuddy.exe"
Name: "{group}\Uninstall HeyBuddy"; Filename: "{uninstallexe}"
Name: "{userdesktop}\HeyBuddy"; Filename: "{app}\HeyBuddy.exe"; Tasks: desktopicon

[Registry]
; Same key/value name the app's Settings toggle uses (utils.win32.enable_autostart).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "HeyBuddy"; ValueData: """{app}\HeyBuddy.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\HeyBuddy.exe"; Description: "Launch HeyBuddy now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM HeyBuddy.exe /F"; Flags: runhidden; RunOnceId: "KillHeyBuddy"

[Code]
var
  RemoveDataCheckbox: Boolean;

function InitializeUninstall(): Boolean;
begin
  { Silent uninstalls (CI, scripted removal) never prompt and keep user data.
    SuppressibleMsgBox also honours /SUPPRESSMSGBOXES, answering No. }
  if UninstallSilent then
    RemoveDataCheckbox := False
  else
    RemoveDataCheckbox := SuppressibleMsgBox('Also delete your HeyBuddy settings, memory and job files in ' +
      ExpandConstant('{userappdata}\HeyBuddy') + '?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveDataCheckbox then
    DelTree(ExpandConstant('{userappdata}\HeyBuddy'), True, True, True);
end;
