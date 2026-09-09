Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress
