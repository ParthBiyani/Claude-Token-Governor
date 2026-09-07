$ErrorActionPreference='Stop'
$ConfigDir=[IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.claude'))
$settings=Join-Path $ConfigDir 'settings.json'
if(Test-Path $settings){
  $stamp=Get-Date -Format 'yyyyMMdd-HHmmss';Copy-Item $settings "$settings.bak-before-uninstall-v3-$stamp"
  $j=Get-Content -Raw $settings|ConvertFrom-Json
  if($j.PSObject.Properties['hooks']){
    foreach($ev in @('UserPromptExpansion','UserPromptSubmit','PostToolBatch','PostCompact','PreToolUse','SubagentStart','SubagentStop','SessionStart')){
      if($j.hooks.PSObject.Properties[$ev]){$j.hooks.($ev)=@($j.hooks.($ev)|Where-Object { $_.hooks.command -notmatch 'token-governor.*governor_hook\.py' })}
    }
  }
  $j|ConvertTo-Json -Depth 40|Set-Content -Encoding UTF8 $settings
}
Remove-Item -Recurse -Force (Join-Path $ConfigDir 'token-governor') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $ConfigDir 'skills\limit') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $ConfigDir 'skills\token-limit') -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $ConfigDir 'claude-b.ps1') -ErrorAction SilentlyContinue
Write-Host 'Claude Token Governor v3 removed from .claude. Backups were kept.' -ForegroundColor Green
