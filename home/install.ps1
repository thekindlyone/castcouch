# Installs the castcouch home service for the current Windows user, starts it, and has Windows start it
# at every sign-in. A supervisor restarts the service if it exits or stops answering; Task Scheduler
# starts the supervisor again within 5 minutes if that ever exits. Needs no administrator rights.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Python C:\path\to\python.exe] [-Port 8899] [-Library D:\Movies,E:\Movies] [-Force]
# Settings live in config.json beside the service: film folders ("library") and an optional "opensubtitles_api_key".
# It will not replace a running service while something is playing on the TV, unless -Force.
param([string]$Python = '', [int]$Port = 8899, [string[]]$Library = @(), [switch]$Force)
$ErrorActionPreference = 'Stop'
$name = 'castcouch-home'
# Not under AppData: packaged apps (and tools they start) can have AppData writes redirected out of sight.
$dir = Join-Path $env:USERPROFILE 'castcouch'

if (-not $Python) {
  $Python = (Get-Command python.exe -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1).Source
}
if (-not $Python -or -not (Test-Path $Python)) { throw 'Python 3.9+ not found; pass -Python C:\path\to\python.exe' }
$pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $Python" }

# Do not interrupt a film: the running copy may be feeding the TV right now.
if (-not $Force) {
  try { $now = Invoke-RestMethod -TimeoutSec 15 "http://127.0.0.1:$Port/status" } catch { $now = $null }
  if ($now.playing) { throw "Not updating: '$($now.title)' is $($now.state.ToLower()) on $($now.tv). Try again when it has finished, or use -Force." }
}

New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot 'castcouch_home.py') $dir

# Settings: created once, then left to you (folders passed with -Library replace the list).
$config = Join-Path $dir 'config.json'
$settings = if (Test-Path $config) { Get-Content -Raw $config | ConvertFrom-Json } else { [pscustomobject]@{ library = @(); opensubtitles_api_key = '' } }
# With -File, "D:\Movies,E:\Movies" arrives as one string: split it.
$Library = @($Library | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($Library.Count) { $settings.library = @($Library | Where-Object { Test-Path $_ }) }
[IO.File]::WriteAllText($config, ($settings | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

# Replace any running copy.
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%.exe'" | Where-Object { $_.CommandLine -match 'castcouch_home\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$script = Join-Path $dir 'castcouch_home.py'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" --supervise --port $Port --log `"$dir\home.log`"" -WorkingDirectory $dir
# At sign-in, and every 5 minutes as a safety net: a second supervisor exits at once if one is running.
$trigger = @(
  (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
  (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description 'castcouch: plays videos from the phone on the TV (see github.com/thekindlyone/castcouch)' -Force | Out-Null
Start-ScheduledTask -TaskName $name

for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 500
  try { $r = Invoke-RestMethod -TimeoutSec 2 "http://127.0.0.1:$Port/ping"; if ($r.ok) { break } } catch {}
}
if (-not $r.ok) { throw "Installed, but the service is not answering on port $Port; see $dir\home.log" }
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -eq 'Dhcp' } | Select-Object -First 1).IPAddress
"castcouch home $($r.version) is running (pid $($r.pid)); TV: $($r.tv)"
"Phone address: http://${ip}:$Port/   Log: $dir\home.log   Settings: $config"
