# Prepare and start an isolated LabVIEW instance for validating generated files.
#
# The instance gets its own ini (copied from the installation's, plus overrides), its own
# LabVIEW Data folder (so LVAutoSave / crash recovery / internal logs are separate) and its
# own compiled-object cache, so it can run next to a LabVIEW you are working in without
# sharing objFileDB.vidb. Nothing it does pops up dialogs in your session.
#
#   lvinstance.ps1 -Work C:\tmp\lvtest [-Port 3683] [-LabVIEW "C:\Program Files\National Instruments\LabVIEW 2025\LabVIEW.exe"]
param(
  [Parameter(Mandatory)][string]$Work,
  [int]$Port = 3683,
  [string]$LabVIEW = "C:\Program Files\National Instruments\LabVIEW 2025\LabVIEW.exe"
)
$Work = (New-Item -ItemType Directory -Force $Work).FullName
$data = (New-Item -ItemType Directory -Force (Join-Path $Work "lvdata")).FullName
$cache = (New-Item -ItemType Directory -Force (Join-Path $Work "objcache")).FullName
$ini = Join-Path $Work "LabVIEW-isolated.ini"

if (netstat -ano | Select-String ":$Port\s.*LISTENING") { "port $Port already listening"; return }

$src = Get-Content (Join-Path (Split-Path $LabVIEW) "LabVIEW.ini")
$drop = '^(server\.|AllowMultipleInstances|DefaultDataFileLocation|CompiledObjCacheRoot|showWelcomeOnLaunch|neverShowAddonLicensingStartup)'
$out = @(
  "[LabVIEW]",
  "AllowMultipleInstances=True",
  "DefaultDataFileLocation=`"$data`"",
  "CompiledObjCacheRoot=`"$cache`"",
  "server.tcp.enabled=True",
  "server.tcp.port=$Port",
  "server.tcp.access=`"+127.0.0.1;+localhost`"",
  "showWelcomeOnLaunch=False",
  "neverShowAddonLicensingStartup=True"
) + ($src | Select-Object -Skip 1 | Where-Object { $_ -notmatch $drop })
$out | Set-Content -Encoding ascii $ini

$p = Start-Process $LabVIEW -ArgumentList "-pref `"$ini`"" -PassThru
$deadline = (Get-Date).AddSeconds(120)
do { Start-Sleep 2 } until ((netstat -ano | Select-String ":$Port\s.*LISTENING") -or (Get-Date) -gt $deadline)
"started LabVIEW pid $($p.Id) on port $Port (ini: $ini)"
