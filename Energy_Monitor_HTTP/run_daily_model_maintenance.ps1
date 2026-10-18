$ErrorActionPreference = 'Stop'

# Run the collector first so PostgreSQL receives the latest available actuals,
# then run controlled model training. The approved artifact is promoted only
# when the training script's validation and new-data checks pass.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ScriptDir

$LogDir = Join-Path $ScriptDir 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogPath = Join-Path $LogDir 'daily_model_maintenance.log'
$PythonCommand = Get-Command python.exe -ErrorAction Stop
$PythonPath = $PythonCommand.Source

$started = Get-Date
"`r`n=== Energy Monitor maintenance started: $($started.ToString('yyyy-MM-dd HH:mm:ss zzz')) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
"Python: $PythonPath" | Out-File -FilePath $LogPath -Append -Encoding utf8

# Step 1: collect actual readings and persist them to PostgreSQL.
$collectorOutput = @(& $PythonPath '.\collect_once.py' 2>&1)
$collectorExitCode = $LASTEXITCODE
$collectorOutput | Out-File -FilePath $LogPath -Append -Encoding utf8
$collectorText = ($collectorOutput | ForEach-Object { $_.ToString() }) -join "`n"
if ($collectorExitCode -ne 0 -or $collectorText -notmatch 'PostgreSQL: data saved successfully') {
    "ERROR: Collector failed or did not confirm PostgreSQL save. Training was skipped." | Out-File -FilePath $LogPath -Append -Encoding utf8
    Get-Content -LiteralPath $LogPath -Tail 60
    exit 1
}

# Step 2: train/evaluate with actual DB data. The trainer protects the approved
# model when there is no newer actual date or validation checks do not pass.
$trainingOutput = @(& $PythonPath '.\train_daily_model_controlled.py' 2>&1)
$trainingExitCode = $LASTEXITCODE
$trainingOutput | Out-File -FilePath $LogPath -Append -Encoding utf8
if ($trainingExitCode -ne 0) {
    "ERROR: Controlled model training exited with code $trainingExitCode." | Out-File -FilePath $LogPath -Append -Encoding utf8
    Get-Content -LiteralPath $LogPath -Tail 80
    exit $trainingExitCode
}

$finished = Get-Date
"=== Energy Monitor maintenance finished: $($finished.ToString('yyyy-MM-dd HH:mm:ss zzz')) ===" | Out-File -FilePath $LogPath -Append -Encoding utf8
Get-Content -LiteralPath $LogPath -Tail 80
exit 0
