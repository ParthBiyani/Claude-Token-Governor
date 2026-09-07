$inputText = [Console]::In.ReadToEnd()
try { $j = $inputText | ConvertFrom-Json } catch { exit 0 }
$cfg = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$gov = Join-Path $cfg 'token-governor/governor.py'
if (Test-Path $gov) {
  $payloadFile = Join-Path $cfg 'token-governor/statusline-last.json'
  try { $inputText | Set-Content -Encoding UTF8 -LiteralPath $payloadFile } catch {}
  try {
    $inputText | & python $gov ingest-statusline 2>$null | Out-Null
  } catch {}
}

$model = if ($j.model.display_name) { [string]$j.model.display_name } else { 'Claude' }
$ctx = if ($null -ne $j.context_window.used_percentage) { '{0:0}' -f [double]$j.context_window.used_percentage } else { '--' }
$fh = if ($null -ne $j.rate_limits.five_hour.used_percentage) { '{0:0}' -f [double]$j.rate_limits.five_hour.used_percentage } else { '--' }
$govState='off'; $govPct='--'
try {
  $sid=[string]$j.session_id
  $raw = & python $gov check --session $sid --transcript ([string]$j.transcript_path) 2>$null
  if ($raw) {
    $s=$raw | ConvertFrom-Json
    if ($s.enabled) {
      if ($s.hard_budget_tokens) { $govPct='{0:0}' -f (($s.weighted_tokens_used / $s.hard_budget_tokens)*100) }
      $govState = if ($s.hard_stop) {'STOP'} elseif ($s.ship_mode) {'SHIP'} else {'ON'}
    }
  }
} catch {}
Write-Output ("{0} | 5h {1}% | GOV {2}% {3} | ctx {4}%" -f $model,$fh,$govPct,$govState,$ctx)
