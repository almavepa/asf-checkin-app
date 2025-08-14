
[Setup]
AppName=Checkin System
AppVersion=1.0.3
DefaultDirName={userappdata}\ASFormacao\Checkin
DefaultGroupName=ASFormacao\Checkin
OutputDir=dist
OutputBaseFilename=CheckinSetup-v1.0.3
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes


[Files]
Source: "dist\CheckinApp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\updater_install.exe"; DestDir: "{app}"; Flags: ignoreversion


[Icons]
Name: "{group}\Checkin System"; Filename: "{app}\CheckinApp.exe"
Name: "{commondesktop}\Checkin System"; Filename: "{app}\CheckinApp.exe"

[Run]
Filename: "{app}\CheckinApp.exe"; Flags: nowait postinstall skipifsilent
