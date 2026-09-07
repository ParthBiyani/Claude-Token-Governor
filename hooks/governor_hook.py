#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

CONFIG_DIR=Path(os.environ.get('CLAUDE_CONFIG_DIR',str(Path.home()/'.claude'))).expanduser()
GOV=CONFIG_DIR/'token-governor'/'governor.py'

def run(*args):
    try:
        p=subprocess.run([sys.executable,str(GOV),*args],capture_output=True,text=True,timeout=10)
        return p.returncode,p.stdout.strip(),p.stderr.strip()
    except Exception as e:return 1,'',str(e)

def emit(x): print(json.dumps(x,separators=(',',':')))

def main():
    try:e=json.load(sys.stdin)
    except Exception:return 0
    sid=e.get('session_id'); ev=e.get('hook_event_name')
    if not sid:return 0
    tr=e.get('transcript_path') or ''
    extra=[]
    if e.get('agent_transcript_path'):extra.append(str(e['agent_transcript_path']))

    # The slash command itself is consumed locally. It does not become a model prompt.
    if ev=='UserPromptExpansion' and e.get('command_name')=='token-limit':
        args=(e.get('command_args') or '').strip()
        parts=args.split()
        if not parts:
            reason='Usage: /token-limit 50 45 | /token-limit status | on | off | reset. Optional third argument is an explicit 5h token-equivalent cap.'
            emit({'decision':'block','reason':reason});return 0
        action=parts[0].lower()
        if action in ('status','on','off','reset') and len(parts)==1:
            rc,out,err=run(action,'--session',sid,'--transcript',tr)
            emit({'decision':'block','reason':out if rc==0 else (err or 'Token Governor failed.')});return 0
        try:
            if len(parts) not in (2,3):raise ValueError('Expected: /token-limit <budget%> <ship%> [5h_token_cap].')
            b=float(parts[0]);s=float(parts[1]);cap=int(parts[2]) if len(parts)==3 else None
            cmd=['configure','--session',sid,'--budget-pct',str(b),'--ship-pct',str(s)]
            if cap:cmd += ['--cap',str(cap)]
            rc,out,err=run(*cmd)
            emit({'decision':'block','reason':out if rc==0 else (err or 'Token Governor configuration failed.')})
        except Exception as ex:emit({'decision':'block','reason':str(ex)})
        return 0

    # Refresh the server-side 5h meter at most once per 60s. This is a read-only
    # usage request and is not an inference call. It also works if the user keeps an existing custom status line.
    if ev in ('SessionStart','PostToolBatch','PostCompact','UserPromptSubmit'):
        run('status','--session',sid,'--transcript',tr)

    # Reconcile locally recorded usage. No network call is made here.
    if tr:
        cmd=['reconcile','--session',sid,'--transcript',tr]
        for x in extra:cmd += ['--subagent-transcript',x]
        run(*cmd)
    rc,out,err=run('check','--session',sid,'--transcript',tr,*sum(([ '--subagent-transcript',x] for x in extra),[]))
    if rc!=0:return 0
    try:st=json.loads(out)
    except Exception:return 0
    if not st.get('enabled'):return 0

    used=int(st.get('weighted_tokens_used') or 0); hard=st.get('hard_budget_tokens'); ship=st.get('ship_budget_tokens')
    margin=int(st.get('safety_margin_tokens') or 10000)
    hard_wall=max(0,int(hard)-margin) if hard else None

    account_stop=bool(st.get('account_rate_limit_backstop'))
    if ev=='PreToolUse' and ((hard_wall is not None and used>=hard_wall) or account_stop):
        if account_stop:
            reason=(f"TOKEN GOVERNOR ACCOUNT BACKSTOP: Anthropic 5-hour account usage is {float(st.get('five_hour_used_percent')):.1f}%. Further tool calls are blocked to avoid the account-wide rate limit. Finalize/ship the current deliverable.")
        else:
            reason=(f'TOKEN GOVERNOR HARD WALL: {used:,} weighted tokens used; session budget {int(hard):,}; '
                    f'safety margin {margin:,}. Tool call denied. Stop expanding scope and finalize/ship the requested deliverable.')
        emit({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':reason},'systemMessage':reason})
        return 0

    if ev=='SubagentStart' and hard is not None:
        # Very short: this reminder itself is intentionally kept small so the governor does not consume
        # meaningful quota just by enforcing the budget.
        remaining=max(0,int(hard)-used)
        msg=f'Budget active: {used:,}/{int(hard):,} weighted tokens; subagent usage counts toward the same parent budget. Work narrowly and finish the assigned task.'
        emit({'hookSpecificOutput':{'hookEventName':'SubagentStart','additionalContext':msg}})
        return 0

    if ev in ('UserPromptSubmit','PostToolBatch','PostCompact','SubagentStop') and hard is not None:
        if ship is not None and used>=int(ship):
            msg=(f'SHIP MODE: {used:,}/{int(hard):,} weighted tokens. Finish the core deliverable, run only essential checks, '
                 'fix critical failures, and ship. Do not start new features or broad refactors.')
            emit({'hookSpecificOutput':{'hookEventName':ev,'additionalContext':msg},'systemMessage':msg})
        elif ev=='UserPromptSubmit':
            # Do not inject a per-turn meter: the status line is local and the transcript accounting is automatic.
            pass
    return 0
if __name__=='__main__':sys.exit(main())
