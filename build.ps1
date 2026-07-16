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

# 清理旧构建产物，避免正在开发时重复打包混入旧文件。
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
  --add-data "$Vendor;vendor" `
  --add-data "$Assets;assets" `
  "run.py"

Write-Host "打包完成：$Root\dist\DocContentSearch\DocContentSearch.exe"
