# Installs the NT8Bridge AddOn: copies EVERY addon\*.cs into NT8's Custom\AddOns folder, and deletes installed
# NT8Bridge*.cs files that no longer exist in the repo. One orphan partial keeps NinjaTrader.Custom red, and a
# red NinjaTrader.Custom unloads every custom indicator, strategy and AddOn the user has.
# NT8 recompiles by itself 20-150 s after the files land (F5 in the NinjaScript Editor forces it).
# Run: powershell -ExecutionPolicy Bypass -File scripts\install-addon.ps1
$ErrorActionPreference = 'Stop'
$src  = Join-Path $PSScriptRoot '..\addon'
$dest = "$env:USERPROFILE\Documents\NinjaTrader 8\bin\Custom\AddOns"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# -Filter '*.cs' also matches '.csproj'-style extensions (8.3 names), hence the explicit extension test.
$files = @(Get-ChildItem -Path $src -Filter '*.cs' -File | Where-Object { $_.Extension -eq '.cs' })
if ($files.Count -eq 0) { throw "no .cs files in $src" }
$names = @($files | ForEach-Object { $_.Name })

Get-ChildItem -Path $dest -Filter 'NT8Bridge*.cs' -File |
	Where-Object { $_.Extension -eq '.cs' -and $names -notcontains $_.Name } |
	ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force; Write-Output "removed orphan $($_.Name)" }

foreach ($f in $files) { Copy-Item -LiteralPath $f.FullName -Destination $dest -Force; Write-Output "installed $($f.Name)" }
Write-Output "done: $($files.Count) file(s) in $dest - NT8 recompiles by itself in 20-150 s"
