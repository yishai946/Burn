; Burner installer (Inno Setup 6). Build: build.bat in the burner folder.
; Per-user install, no admin needed: %LOCALAPPDATA%\Programs\Burner

#define AppName "Burner"
#ifndef AppVersion
  #define AppVersion "dev"  ; build.bat passes the real one from core.VERSION
#endif

[Setup]
AppId={{6F1C2A4E-8B7D-4C1E-9A53-2D7E5B0C9F11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Burner-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\Burner.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; the app's single-instance mutex: setup asks to close Burner before updating it
AppMutex=Burner.SingleInstance

[Languages]
Name: "hebrew"; MessagesFile: "compiler:Languages\Hebrew.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Burner\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID matches the app, so the taskbar shows Burner's icon and groups its window with the shortcut
Name: "{group}\{#AppName}"; Filename: "{app}\Burner.exe"; AppUserModelID: "Burner.App"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Burner.exe"; AppUserModelID: "Burner.App"; Tasks: desktopicon

[Run]
Filename: "{app}\Burner.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; in-app updates run setup silently: reopen Burner when it finishes
Filename: "{app}\Burner.exe"; Flags: nowait; Check: WizardSilent

[Code]
// Burner's window is drawn by Microsoft Edge WebView2. Windows 11 has it; very old Windows 10 may not.
function HasWebView2(): Boolean;
var v: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var code: Integer;
begin
  if (CurStep = ssPostInstall) and not HasWebView2() then
    if MsgBox('כדי ש־Burner יעבוד צריך את רכיב Microsoft Edge WebView2, והוא לא נמצא במחשב.' + #13#10 +
              'לפתוח את דף ההורדה הרשמי של Microsoft עכשיו?', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703', '', '', SW_SHOWNORMAL, ewNoWait, code);
end;
