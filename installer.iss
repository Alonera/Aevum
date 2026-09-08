; Inno Setup script for Aevum
; Compile with: ISCC.exe installer.iss  (produces Aevum-Setup.exe)

#define AppName "Aevum"
#define AppVersion "1.2.7"
#define AppExe "Aevum.exe"
#ifndef AppSource
  #define AppSource "Portable\\Aevum.exe"
#endif

[Setup]
AppId={{A3E9F1C2-7B4D-4E6A-9C21-AEV000000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Alonera
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=Setup
OutputBaseFilename=Aevum-Setup
SetupIconFile=app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
english.StartMenuIcon=Create a Start Menu shortcut
turkish.StartMenuIcon=Başlat menüsü kısayolu oluştur
spanish.StartMenuIcon=Crear un acceso directo en el menú Inicio
german.StartMenuIcon=Verknüpfung im Startmenü erstellen
french.StartMenuIcon=Créer un raccourci dans le menu Démarrer
italian.StartMenuIcon=Crea un collegamento nel menu Start
portuguese.StartMenuIcon=Criar um atalho no menu Iniciar
russian.StartMenuIcon=Создать ярлык в меню «Пуск»

[Tasks]
Name: "startmenuicon"; Description: "{cm:StartMenuIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppSource}"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startmenuicon
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
