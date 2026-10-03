; OpenEVP: all-in-one installer (Inno Setup 6).
;
; One setup file, one admin prompt, nothing else to install:
;   - the desktop app and the command-line downloader
;   - the recorder driver (WinUSB for every recorder model in the driver manifest,
;     _internal\driver\models.json: today USB 054C:0103, the ICD-ST25 and ICD-ST10), whether or
;     not the recorder is plugged in; Windows applies it when it is (any USB port)
;   - Microsoft's WebView2 runtime, only if this PC does not have it yet
;     (Windows 11 and updated Windows 10 already do; this step needs internet)
; Uninstalling removes all of it, including the driver and its certificate.
;
; Built by build_windows.ps1:  ISCC /DAppVersion=<version> installer\openevp.iss
; Exit code: 0 OK; 1 the recorder driver could not be set up (the app is installed
; and its "Set up recorder" button can retry); 2 WebView2 could not be installed (the
; app cannot open until it is); Inno's own codes otherwise.

#ifndef AppVersion
  #error Pass /DAppVersion=<version> (build_windows.ps1 does)
#endif
; The driver scripts read the manifest; without it the driver step would fail on every PC.
#if !FileExists(AddBackslash(SourcePath) + "..\dist\OpenEVP\_internal\driver\models.json")
  #error dist\OpenEVP\_internal\driver\models.json is missing: build with build_windows.ps1
#endif

[Setup]
AppId={{4E6B2C5A-8F31-4D7B-9A0E-2C57D1B3F925}
AppName=OpenEVP
AppVersion={#AppVersion}
AppPublisher=Ghost Hunters of South Florida
AppPublisherURL=https://github.com/MBarc/OpenEVP
DefaultDirName={autopf}\OpenEVP
; Upgrades from 0.5.0 ("ST25 Downloader") move to the new folder; [InstallDelete] removes the old one.
UsePreviousAppDir=no
DefaultGroupName=OpenEVP
DisableProgramGroupPage=yes
; Always Program Files: the app's "Set up recorder" button runs the bundled driver
; script with admin rights, so it must live where only administrators can change it.
DisableDirPage=yes
PrivilegesRequired=admin
; x64 Windows only for now: ARM64 PCs (x64 emulation + native WinUSB) are untested.
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
; Windows 10 version 2004 or later: the driver step uses pnputil options added there.
MinVersion=10.0.19041
OutputDir=..\dist
OutputBaseFilename=OpenEVP-Setup-{#AppVersion}
SetupIconFile=..\assets\openevp.ico
WizardSmallImageFile=..\assets\wizard-small-55.bmp,..\assets\wizard-small-69.bmp,..\assets\wizard-small-83.bmp,..\assets\wizard-small-110.bmp
WizardImageFile=..\assets\wizard-large-164.bmp,..\assets\wizard-large-205.bmp,..\assets\wizard-large-246.bmp,..\assets\wizard-large-328.bmp
UninstallDisplayIcon={app}\OpenEVP.exe
UninstallDisplayName=OpenEVP
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
; The app holds this mutex while it runs (app/main.py): setup and uninstall ask the user to close it
; first, instead of leaving files in use behind for a restart. ST25DownloaderRunning is the mutex the
; 0.5.0 app ("ST25 Downloader") holds. Keep it while [InstallDelete] below still removes 0.5.0's folder:
; 0.5.0 has no updater, so its users upgrade by running this setup directly, and if the 0.5.0 app were
; still open its folder could not be deleted.
AppMutex=OpenEVPRunning,Global\OpenEVPRunning,ST25DownloaderRunning,Global\ST25DownloaderRunning

[Messages]
UninstalledAndNeedsRestart=To finish removing the recorder driver from %1, Windows needs to restart.%n%nRestart now? Choose No to restart later yourself; nothing breaks in the meantime.
FinishedRestartMessage=To finish setting up the recorder driver, Windows needs to restart.%n%nRestart now? You can also restart later; plug the recorder in again afterwards.

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; An upgrade replaces the bundled runtime completely, so no stale module survives.
Type: filesandordirs; Name: "{app}\_internal"
; 0.5.0 was called "ST25 Downloader": remove its folder and shortcuts.
Type: filesandordirs; Name: "{autopf}\ST25 Downloader"
Type: files; Name: "{autoprograms}\ST25 Downloader.lnk"
Type: files; Name: "{autodesktop}\ST25 Downloader.lnk"
; 0.9.9 and earlier installed the command-line tool as openevp-st25.exe; it is openevp-cli.exe now.
Type: files; Name: "{app}\command-line\openevp-st25.exe"

[Files]
Source: "..\dist\OpenEVP\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\openevp-cli.exe"; DestDir: "{app}\command-line"; Flags: ignoreversion
Source: "..\vendor\webview2\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{autoprograms}\OpenEVP"; Filename: "{app}\OpenEVP.exe"; AppUserModelID: "MBarc.OpenEVP"
Name: "{autodesktop}\OpenEVP"; Filename: "{app}\OpenEVP.exe"; AppUserModelID: "MBarc.OpenEVP"; Tasks: desktopicon

[Run]
Filename: "{app}\OpenEVP.exe"; Description: "Start OpenEVP"; Flags: postinstall nowait skipifsilent; Check: HasWebView2
; An update started from the app (/SILENT /RELAUNCH) reopens it, as the signed-in user, not as admin.
Filename: "{app}\OpenEVP.exe"; Flags: nowait runasoriginaluser skipifnotsilent; Check: HasWebView2 and IsRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{app}\driver-setup"
Type: dirifempty; Name: "{app}"

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

var
  DriverFailed: Boolean;
  DriverWantsRestart: Boolean;
  WebView2Failed: Boolean;
  UninstallWantsRestart: Boolean;

function HasWebView2In(Key: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(HKLM, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

(* Machine-wide only: a per-user WebView2 of the administrator running setup would
   not help other users of this machine-wide app. *)
function HasWebView2: Boolean;
begin
  Result := HasWebView2In('SOFTWARE\WOW6432Node\' + WebView2Key) or HasWebView2In('SOFTWARE\' + WebView2Key);
end;

(* Set by the app's updater (app/updater.py launch()). *)
function IsRelaunch: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RELAUNCH') = 0 then
      Result := True;
end;

function NeedsWebView2: Boolean;
begin
  Result := not HasWebView2;
end;

(* 64-bit PowerShell: the driver tools (Get-WindowsDriver, pnputil) must not run under WOW64.
   In 64-bit install mode {sys} is the real System32 and Exec runs without redirection.
   Do not use {sysnative}: PowerShell started through that alias cannot set its own
   folder as the current directory, and Get-WindowsDriver (DISM) then fails. *)
function RunDriverScript(Name: String): Integer;
var
  Code: Integer;
begin
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\_internal\driver\') + Name + '"',
              '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  Result := Code;
end;

function LogTail(FileName: String; Lines: Integer): String;
var
  All: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if LoadStringsFromFile(FileName, All) then
    for I := GetArrayLength(All) - Lines to GetArrayLength(All) - 1 do
      if I >= 0 then
        Result := Result + All[I] + #13#10;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  if NeedsWebView2 then
  begin
    WizardForm.StatusLabel.Caption := 'Installing the Microsoft WebView2 runtime...';
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '',
                SW_HIDE, ewWaitUntilTerminated, Code) then
      Code := -1;
    WebView2Failed := not HasWebView2;
    if WebView2Failed then
      SuppressibleMsgBox('The Microsoft WebView2 runtime could not be installed (code ' + IntToStr(Code) + '). ' +
                         'OpenEVP needs it to open its window. Connect this PC to the internet and ' +
                         'run this setup again.', mbError, MB_OK, IDOK);
  end;

  WizardForm.StatusLabel.Caption := 'Setting up the recorder driver...';
  Code := RunDriverScript('install-winusb.ps1');
  DriverWantsRestart := Code = 3010;
  DriverFailed := (Code <> 0) and (Code <> 3010);
  if DriverFailed then
    SuppressibleMsgBox('The recorder driver could not be set up (code ' + IntToStr(Code) + '):' + #13#10#13#10 +
                       LogTail(ExpandConstant('{app}\driver-setup\setup.log'), 6) + #13#10 +
                       'You can retry from the app: plug in the recorder and click "Set up recorder".',
                       mbError, MB_OK, IDOK);
end;

function NeedRestart: Boolean;
begin
  Result := DriverWantsRestart;
end;

function GetCustomSetupExitCode: Integer;
begin
  if WebView2Failed then
    Result := 2
  else if DriverFailed then
    Result := 1
  else
    Result := 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  (* usUninstall comes before any file is removed, so the script and its log are still there. *)
  if CurUninstallStep <> usUninstall then
    Exit;
  Code := RunDriverScript('uninstall-winusb.ps1');
  UninstallWantsRestart := Code = 3010;
  if (Code <> 0) and (Code <> 3010) then
    SuppressibleMsgBox('The recorder driver or its certificate could not be removed completely (code ' +
                       IntToStr(Code) + '):' + #13#10#13#10 +
                       LogTail(ExpandConstant('{app}\driver-setup\uninstall.log'), 8),
                       mbError, MB_OK, IDOK);
end;

function UninstallNeedRestart: Boolean;
begin
  Result := UninstallWantsRestart;
end;
