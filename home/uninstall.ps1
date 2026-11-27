# Stops the castcouch home service and removes it, its scheduled task and its files.
$ErrorActionPreference = 'Continue'
Stop-ScheduledTask -TaskName castcouch-home -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName castcouch-home -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%.exe'" | Where-Object { $_.CommandLine -match 'castcouch_home\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE 'castcouch') -ErrorAction SilentlyContinue
'castcouch home service removed.'
