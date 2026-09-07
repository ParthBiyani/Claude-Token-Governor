$ErrorActionPreference='Stop'
function Find-Python {
  foreach($n in @('python','py')) { try { $c=Get-Command $n -ErrorAction Stop; if($c){return $n} } catch {} }
  throw 'Python 3.9+ is required. Install Python and ensure python/py is on PATH.'
}
$Python=Find-Python
$ConfigDir=[IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.claude'))
$Root=Join-Path $ConfigDir 'token-governor';$Hooks=Join-Path $Root 'hooks';$State=Join-Path $Root 'state'
New-Item -ItemType Directory -Force -Path $Root,$Hooks,$State,(Join-Path $ConfigDir 'skills\limit'),(Join-Path $ConfigDir 'skills\token-limit') | Out-Null
$src=Split-Path -Parent $MyInvocation.MyCommand.Path
Copy-Item "$src\governor.py" "$Root\governor.py" -Force
Copy-Item "$src\statusline.ps1" "$Root\statusline.ps1" -Force
if(-not (Test-Path (Join-Path $Root 'config.json'))){ Copy-Item "$src\config.example.json" (Join-Path $Root 'config.json') -Force }
Copy-Item "$src\hooks\governor_hook.py" "$Hooks\governor_hook.py" -Force
Copy-Item "$src\skill\limit\SKILL.md" (Join-Path $ConfigDir 'skills\limit\SKILL.md') -Force
Copy-Item "$src\skill\token-limit\SKILL.md" (Join-Path $ConfigDir 'skills\token-limit\SKILL.md') -Force

$settings=Join-Path $ConfigDir 'settings.json'
if(Test-Path $settings){$stamp=Get-Date -Format 'yyyyMMdd-HHmmss';Copy-Item $settings "$settings.bak-token-governor-v3-$stamp"; $j=Get-Content -Raw $settings|ConvertFrom-Json}else{$j=[pscustomobject]@{}}
if(-not $j.PSObject.Properties['hooks']){$j|Add-Member NoteProperty hooks ([pscustomobject]@{})}
function Ensure($obj,$name){if(-not $obj.PSObject.Properties[$name]){$obj|Add-Member NoteProperty $name @()}}
$hookCmd='python "'+(Join-Path $Hooks 'governor_hook.py')+'"'
foreach($spec in @(
  @{Event='UserPromptExpansion';Matcher='limit'},
  @{Event='UserPromptExpansion';Matcher='token-limit'},
  @{Event='UserPromptSubmit';Matcher=''},
  @{Event='PostToolBatch';Matcher=''},
  @{Event='PostCompact';Matcher=''},
  @{Event='PreToolUse';Matcher=''},
  @{Event='SubagentStart';Matcher=''},
  @{Event='SubagentStop';Matcher=''},
  @{Event='SessionStart';Matcher=''}
)){
  Ensure $j.hooks $spec.Event
  $arr=@($j.hooks.($spec.Event));$exists=$false
  foreach($e in $arr){foreach($h in @($e.hooks)){if([string]$h.command -eq $hookCmd -and [string]$e.matcher -eq [string]$spec.Matcher){$exists=$true}}}
  if(-not $exists){$entry=[pscustomobject]@{matcher=$spec.Matcher;hooks=@([pscustomobject]@{type='command';command=$hookCmd;timeout=15})};$j.hooks.($spec.Event)=@($arr+$entry)}
}
# Prefer our statusline only if there isn't one. If the user already has one, the SessionStart/PostToolBatch hooks
# still poll the usage endpoint (cached for 60s), so the governor remains functional.
if(-not $j.PSObject.Properties['statusLine']){
  $cmd='powershell -NoProfile -File "'+(Join-Path $Root 'statusline.ps1')+'"'
  $j|Add-Member NoteProperty statusLine ([pscustomobject]@{type='command';command=$cmd;padding=0;refreshInterval=30})
}
$j|ConvertTo-Json -Depth 40|Set-Content -Encoding UTF8 $settings

$launcher=Join-Path $ConfigDir 'claude-b.ps1'
@"
`$env:CLAUDE_CONFIG_DIR = '$ConfigDir'
& claude @args
"@|Set-Content -Encoding UTF8 $launcher
$env:CLAUDE_CONFIG_DIR=$ConfigDir
& $Python "$Root\governor.py" self-test
Write-Host ''
Write-Host 'Claude Token Governor v3 installed in .claude' -ForegroundColor Green
Write-Host 'Installed into your existing .claude configuration.' -ForegroundColor Cyan
Write-Host ''
Write-Host 'Launch this account with:' -ForegroundColor Yellow
Write-Host "  & `"$launcher`"" -ForegroundColor Cyan
Write-Host ''
Write-Host 'Inside Claude Code:' -ForegroundColor Yellow
Write-Host '  /limit 50 45'
Write-Host '  /limit status'
Write-Host '  /limit off'
Write-Host '  /limit on'
Write-Host '  /limit reset'
Write-Host ''
Write-Host 'The governor uses the account 5-hour rolling rate-limit meter, not context-window size.' -ForegroundColor Green
Write-Host 'The server exposes utilization percentage, not a fixed raw-token denominator; v3 auto-detects plan tier and uses a clearly-labelled token-equivalent estimate for the per-session hard wall.' -ForegroundColor Yellow
