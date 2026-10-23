# Close the isolated LabVIEW instance cleanly through LabVIEWCLI (a forced kill leaves a
# crash-recovery prompt for the next start).
param(
  [int]$Port = 3683,
  [string]$LabVIEW = "C:\Program Files\National Instruments\LabVIEW 2025\LabVIEW.exe",
  [string]$Cli = "C:\Program Files (x86)\National Instruments\Shared\LabVIEW CLI\LabVIEWCLI.exe"
)
& $Cli -OperationName CloseLabVIEW -PortNumber $Port -LabVIEWPath $LabVIEW 2>&1 | Select-String "succeeded|failed"
