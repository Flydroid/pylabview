# Mass-compile a directory in the isolated LabVIEW instance and report
#  - Bad VIs from the mass-compile log, and
#  - internal warnings (DWarn / DAbort) LabVIEW wrote to its lvlog during the run.
# A clean mass compile alone is not enough: front-panel "insanities" are only logged.
#
#   lvcheck.ps1 -Dir <folder with .ctl files> -Work <work folder of lvinstance.ps1> [-Port 3683] [-Fresh]
#
# -Fresh restarts the instance first (and closes it afterwards). Use it for anything you want
# to trust: one LabVIEW session that has loaded several different files with the same qualified
# name (e.g. many generated "Status--cluster.ctl") can log warnings that are not about the file.
param(
  [Parameter(Mandatory)][string]$Dir,
  [Parameter(Mandatory)][string]$Work,
  [int]$Port = 3683,
  [switch]$Fresh,
  [string]$LabVIEW = "C:\Program Files\National Instruments\LabVIEW 2025\LabVIEW.exe",
  [string]$Cli = "C:\Program Files (x86)\National Instruments\Shared\LabVIEW CLI\LabVIEWCLI.exe"
)
$here = $PSScriptRoot
function Listening { [bool](netstat -ano | Select-String ":$Port\s.*LISTENING") }
function Close-Instance {
  if (Listening) { & (Join-Path $here "lvclose.ps1") -Port $Port -LabVIEW $LabVIEW -Cli $Cli | Out-Null }
  $deadline = (Get-Date).AddSeconds(60)
  while ((Listening) -and (Get-Date) -lt $deadline) { Start-Sleep 1 }
  Start-Sleep 2  # let the process exit after the port closes
}

if ($Fresh) { Close-Instance }
if (-not (Listening)) { & (Join-Path $here "lvinstance.ps1") -Work $Work -Port $Port -LabVIEW $LabVIEW | Out-Null; Start-Sleep 3 }

$logs = Join-Path $Work "lvdata\LVInternalReports"
# Logged by LabVIEW at startup on this machine regardless of the files compiled
$noise = 'MinMaxInc cluster TD'

function Snapshot { $h = @{}; Get-ChildItem $logs -Recurse -Filter "lvlog*.txt" -ErrorAction SilentlyContinue | ForEach-Object { $h[$_.FullName] = (Get-Content $_.FullName).Count }; $h }
$before = Snapshot
$log = Join-Path $env:TEMP ("lvcheck_" + [guid]::NewGuid().ToString("N") + ".log")
& $Cli -OperationName MassCompile -DirectoryToCompile $Dir -MassCompileLogFile $log -PortNumber $Port -LabVIEWPath $LabVIEW 2>&1 | Out-Null
$bad = if (Test-Path $log) { Get-Content $log | Select-String "Bad VI|Failed|Error" | ForEach-Object { $_.Line.Trim() } } else { @("no mass compile log (LabVIEW not reachable or aborted - see lvlog)") }
Start-Sleep -Seconds 1
$new = @()
Get-ChildItem $logs -Recurse -Filter "lvlog*.txt" -ErrorAction SilentlyContinue | ForEach-Object {
  $skip = if ($before.ContainsKey($_.FullName)) { $before[$_.FullName] } else { 0 }
  $new += Get-Content $_.FullName | Select-Object -Skip $skip | Where-Object { $_ -match 'DWarn|DAbort|\.cpp' -and $_ -notmatch $noise }
}
if ($Fresh) { Close-Instance }
if ($bad) { "BAD:"; $bad } else { "mass compile: clean" }
if ($new) { "INTERNAL WARNINGS:"; $new | Select-Object -Unique } else { "internal warnings: none" }
if ($bad -or $new) { exit 1 }
