$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Vendor = Join-Path $Root "vendor"
$Assets = Join-Path $Root "assets"
$Icon = Join-Path $Assets "app.ico"
$TikaServer = Join-Path $Vendor "tika-server-standard.jar"
$Jre = Join-Path $Vendor "jre"
$DistZip = Join-Path $Root "DocContentSearch.zip"

New-Item -ItemType Directory -Force $Vendor | Out-Null

if (!(Test-Path $TikaServer)) {
  Invoke-WebRequest `
    -Uri "https://repo1.maven.org/maven2/org/apache/tika/tika-server-standard/3.3.1/tika-server-standard-3.3.1.jar" `
    -OutFile $TikaServer
}

if (!(Test-Path (Join-Path $Jre "bin\java.exe"))) {
  $Api = "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jre/hotspot/normal/eclipse"
  $JreZip = Join-Path $env:TEMP "doc-search-jre.zip"
  Remove-Item $JreZip -Force -ErrorAction SilentlyContinue
  Invoke-WebRequest -Uri $Api -OutFile $JreZip
  $Tmp = Join-Path $env:TEMP "doc-search-jre"
  Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
  Expand-Archive -Path $JreZip -DestinationPath $Tmp -Force
  $JreHome = Get-ChildItem $Tmp -Directory | Select-Object -First 1
  Remove-Item $Jre -Recurse -Force -ErrorAction SilentlyContinue
  Move-Item $JreHome.FullName $Jre
}

# Clean previous build outputs before packaging.
Remove-Item (Join-Path $Root "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "dist") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "DocContentSearch.spec") -Force -ErrorAction SilentlyContinue
Remove-Item $DistZip -Force -ErrorAction SilentlyContinue

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name DocContentSearch `
  --icon "$Icon" `
  --exclude-module PySide6.QtQml `
  --exclude-module PySide6.QtQuick `
  --exclude-module PySide6.QtQuickWidgets `
  --exclude-module PySide6.QtPdf `
  --exclude-module PySide6.QtPdfWidgets `
  --exclude-module PySide6.QtOpenGL `
  --exclude-module PySide6.QtOpenGLWidgets `
  --exclude-module PySide6.QtWebEngineCore `
  --exclude-module PySide6.QtWebEngineWidgets `
  --add-data "$Vendor;vendor" `
  --add-data "$Assets;assets" `
  "run.py"
if ($LASTEXITCODE -ne 0) {
  throw "Build failed. Please exit DocContentSearch from tray and retry."
}

# PyInstaller's PySide6 hook can collect unused Qt modules; keep only the current Widgets UI runtime.
$Internal = Join-Path $Root "dist\DocContentSearch\_internal"
$PySide = Join-Path $Internal "PySide6"
$UnusedQtFiles = @(
  "opengl32sw.dll",
  "Qt6Qml.dll",
  "Qt6QmlMeta.dll",
  "Qt6QmlModels.dll",
  "Qt6QmlWorkerScript.dll",
  "Qt6Quick.dll",
  "Qt6Pdf.dll",
  "Qt6OpenGL.dll",
  "Qt6VirtualKeyboard.dll",
  "QtQml.pyd",
  "QtQuick.pyd",
  "QtQuickWidgets.pyd",
  "QtPdf.pyd",
  "QtPdfWidgets.pyd",
  "QtOpenGL.pyd",
  "QtOpenGLWidgets.pyd"
)
foreach ($Name in $UnusedQtFiles) {
  Remove-Item (Join-Path $PySide $Name) -Force -ErrorAction SilentlyContinue
}
foreach ($Name in @("translations", "qml")) {
  Remove-Item (Join-Path $PySide $Name) -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Build complete: $Root\dist\DocContentSearch\DocContentSearch.exe"
