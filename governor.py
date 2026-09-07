#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, os, re, sys, tempfile, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

CONFIG_DIR = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude'))).expanduser()
ROOT = CONFIG_DIR/'token-governor'; STATE_DIR=ROOT/'state'; CACHE=ROOT/'usage-cache.json'; CONFIG=ROOT/'config.json'
STATE_DIR.mkdir(parents=True, exist_ok=True)
USAGE_URL='https://api.anthropic.com/api/oauth/usage'
PROFILE_URL='https://api.anthropic.com/api/oauth/profile'
BETA='oauth-2025-04-20'
# These are deliberately heuristic token-equivalents, not Anthropic-published quotas.
# The authoritative server signal is five_hour.utilization (%); the token estimate exists
# only to make a per-session hard wall possible. Users can override it with 5hTokenCap.
DEFAULT_PLAN_CAPS={'pro':25_000_000,'max5':125_000_000,'max20':500_000_000}
def plan_caps():
    caps=dict(DEFAULT_PLAN_CAPS)
    try:
        if CONFIG.exists():
            x=json.loads(CONFIG.read_text(encoding='utf-8')); caps.update({k:int(v) for k,v in (x.get('planCaps') or {}).items()})
    except Exception: pass
    return caps
WEIGHTS={'input_tokens':1.0,'cache_creation_input_tokens':1.25,'cache_read_input_tokens':0.1,'output_tokens':5.0}
DEFAULT_MARGIN=10_000
CACHE_TTL=60


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=str(path.parent),text=True)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(data,f,indent=2); f.write('\n')
        os.replace(tmp,path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def safe_sid(s): return re.sub(r'[^A-Za-z0-9_.-]','_',s)
def state_path(sid): return STATE_DIR/(safe_sid(sid)+'.json')

def default_state(sid):
    return {'schema':3,'session_id':sid,'enabled':False,'budget_percent':None,'ship_percent':None,
            'basis':'five_hour_rate_limit','weighted_tokens_used':0,'main_weighted_tokens':0,'subagent_weighted_tokens':0,
            'raw_tokens_used':0,'main_raw_tokens':0,'subagent_raw_tokens':0,'seen':{},'offsets':{},'subagent_count':0,
            'five_hour_used_percent':None,'five_hour_reset':None,'usage_source':None,
            'plan_tier':None,'plan_source':None,'estimated_5h_cap_tokens':None,'cap_source':None,
            'hard_budget_tokens':None,'ship_budget_tokens':None,'safety_margin_tokens':DEFAULT_MARGIN,
            'ship_mode':False,'hard_stop':False,'warning_sent':False,'created_at':time.time(),'last_usage_poll':0}

def load(sid):
    p=state_path(sid)
    if not p.exists(): return default_state(sid)
    try:
        d=json.loads(p.read_text(encoding='utf-8')); b=default_state(sid); b.update(d); return b
    except Exception: return default_state(sid)

def save(d): atomic(state_path(d['session_id']),d)

def token_from_usage(u, weighted=True):
    if not isinstance(u,dict): return 0,0
    raw=sum(int(u.get(k) or 0) for k in WEIGHTS)
    if not weighted: return raw,raw
    w=round(sum(float(u.get(k) or 0)*WEIGHTS[k] for k in WEIGHTS))
    return raw,w

def record_usage(obj):
    if obj.get('type')!='assistant': return None
    msg=obj.get('message') if isinstance(obj.get('message'),dict) else {}
    u=msg.get('usage') if isinstance(msg.get('usage'),dict) else obj.get('usage')
    raw,w=token_from_usage(u)
    ident=obj.get('uuid') or msg.get('id') or obj.get('requestId')
    return (str(ident) if ident else None, raw,w)

def transcript_paths(main, extra=()):
    out=[]; seen=set()
    def add(kind,p):
        if not p:return
        pp=Path(os.path.expanduser(os.path.expandvars(str(p))))
        k=str(pp.resolve()) if pp.exists() else str(pp)
        if k not in seen: out.append((kind,pp));seen.add(k)
    add('main',main)
    if main:
        p=Path(os.path.expanduser(os.path.expandvars(main)))
        sub=p.with_suffix('')/'subagents'
        if sub.exists():
            for f in sorted(sub.glob('agent-*.jsonl')): add('subagent',f)
            # workflow/ nested agent transcripts
            for f in sorted(sub.glob('**/agent-*.jsonl')): add('subagent',f)
    for p in extra: add('subagent',p)
    return out

def reconcile(d,main,extra=()):
    seen=d.get('seen',{}); offsets=d.get('offsets',{})
    mdw=mraw=sdw=sraw=0; paths=transcript_paths(main,extra)
    for kind,p in paths:
        if not p.exists(): continue
        key=str(p); off=int(offsets.get(key,0))
        try: size=p.stat().st_size
        except OSError: continue
        if off>size: off=0
        try:
            with p.open('r',encoding='utf-8',errors='replace') as f:
                f.seek(off)
                while True:
                    pos=f.tell(); line=f.readline()
                    if not line: break
                    try:o=json.loads(line)
                    except json.JSONDecodeError: continue
                    ident,raw,w=record_usage(o)
                    if not raw: continue
                    dedupe=f'{key}::{ident or pos}'
                    # Prefer one usage record per response. If Claude later writes a final
                    # record with the same id, replace the earlier snapshot rather than add it.
                    prev=seen.get(dedupe)
                    if prev is not None:
                        if w>prev['w']:
                            delta_w=w-prev['w']; delta_raw=raw-prev['raw'];
                            if kind=='main': mdw+=delta_w;mraw+=delta_raw
                            else: sdw+=delta_w;sraw+=delta_raw
                            seen[dedupe]={'w':w,'raw':raw}
                    else:
                        seen[dedupe]={'w':w,'raw':raw}
                        if kind=='main': mdw+=w;mraw+=raw
                        else: sdw+=w;sraw+=raw
                offsets[key]=f.tell()
        except OSError: pass
    d['main_weighted_tokens']+=mdw; d['subagent_weighted_tokens']+=sdw
    d['main_raw_tokens']+=mraw; d['subagent_raw_tokens']+=sraw
    d['weighted_tokens_used']=d['main_weighted_tokens']+d['subagent_weighted_tokens']
    d['raw_tokens_used']=d['main_raw_tokens']+d['subagent_raw_tokens']
    d['offsets']=offsets; d['seen']=seen
    d['subagent_count']=sum(1 for k,_ in paths if k=='subagent')
    return d

def credentials():
    p=CONFIG_DIR/'.credentials.json'
    if not p.exists(): return None
    try:
        x=json.loads(p.read_text(encoding='utf-8'))
        return (x.get('claudeAiOauth') or {}).get('accessToken')
    except Exception:return None

def api_get(url):
    tok=credentials()
    if not tok:return None
    req=Request(url,headers={'Authorization':f'Bearer {tok}','anthropic-beta':BETA,'Content-Type':'application/json','User-Agent':'claude-code-token-governor'})
    try:
        with urlopen(req,timeout=8) as r:return json.loads(r.read().decode('utf-8'))
    except Exception:return None

def refresh_usage(d, force=False):
    now=time.time()
    if not force and now-float(d.get('last_usage_poll') or 0)<CACHE_TTL:
        return d
    data=None
    if CACHE.exists() and not force:
        try:
            c=json.loads(CACHE.read_text());
            if now-float(c.get('fetched_at',0))<CACHE_TTL:data=c.get('data')
        except Exception:pass
    if data is None:
        data=api_get(USAGE_URL)
        if data is not None:
            atomic(CACHE,{'fetched_at':now,'data':data})
    if isinstance(data,dict):
        fh=data.get('five_hour') or {}
        if isinstance(fh,dict) and fh.get('utilization') is not None:
            d['five_hour_used_percent']=float(fh['utilization']); d['five_hour_reset']=fh.get('resets_at'); d['usage_source']='oauth_usage'
        d['last_usage_poll']=now
    return d

def detect_plan(d, force=False):
    if d.get('plan_tier') and not force:return d
    p=api_get(PROFILE_URL)
    tier=None
    try:tier=(p.get('organization') or {}).get('rate_limit_tier')
    except Exception:pass
    if tier:
        s=tier.lower()
        if 'max_20' in s or 'max20' in s:tier='max20'
        elif 'max_5' in s or 'max5' in s:tier='max5'
        elif 'pro' in s:tier='pro'
        d['plan_tier']=tier; d['plan_source']='oauth_profile'
        explicit=os.environ.get('CLAUDE_5H_TOKEN_CAP')
        d['estimated_5h_cap_tokens']=int(explicit) if explicit and explicit.isdigit() else plan_caps().get(tier)
        d['cap_source']='explicit_env' if explicit and explicit.isdigit() else 'plan_heuristic'
        if not d.get('cap_source'): d['cap_source']='plan_heuristic'
    return d

def recalc(d):
    cap=d.get('estimated_5h_cap_tokens')
    bp=d.get('budget_percent'); sp=d.get('ship_percent')
    d['hard_budget_tokens']=round(cap*bp/100) if cap and bp is not None else None
    d['ship_budget_tokens']=round(cap*sp/100) if cap and sp is not None else None
    used=d.get('weighted_tokens_used',0)
    d['ship_mode']=bool(d['ship_budget_tokens'] is not None and used>=d['ship_budget_tokens'])
    effective_margin = min(int(d.get('safety_margin_tokens',DEFAULT_MARGIN)), max(1, int(d['hard_budget_tokens']*0.02))) if d.get('hard_budget_tokens') else int(d.get('safety_margin_tokens',DEFAULT_MARGIN))
    d['effective_safety_margin_tokens']=effective_margin
    local_stop=bool(d['hard_budget_tokens'] is not None and used>=max(0,d['hard_budget_tokens']-effective_margin))
    account_stop=bool(d.get('five_hour_used_percent') is not None and float(d['five_hour_used_percent'])>=99.5)
    d['hard_stop']=local_stop or account_stop
    d['account_rate_limit_backstop']=account_stop

def status(d):
    recalc(d)
    used=d['weighted_tokens_used']; hard=d.get('hard_budget_tokens'); ship=d.get('ship_budget_tokens')
    state='HARD STOP' if d['hard_stop'] else 'SHIP MODE' if d['ship_mode'] else 'NORMAL'
    lines=['Token Governor v3',f"  Session: {d['session_id']}",f"  Enabled: {bool(d['enabled'])}",
           '  Basis: Claude 5-hour rolling rate-limit (not context window)',
           f"  5h account usage: {d['five_hour_used_percent']:.1f}%" if d.get('five_hour_used_percent') is not None else '  5h account usage: unavailable',
           f"  5h reset: {d['five_hour_reset']}" if d.get('five_hour_reset') else '  5h reset: unavailable',
           f"  Detected plan: {d.get('plan_tier') or 'unknown'} ({d.get('plan_source') or 'not detected'})"]
    if d.get('estimated_5h_cap_tokens'):
        lines += [f"  Estimated 5h token-equivalent cap: {d['estimated_5h_cap_tokens']:,} (heuristic)",
                  f"  Session budget: {d['budget_percent']}% = {d['hard_budget_tokens']:,} weighted tokens",
                  f"  Ship threshold: {d['ship_percent']}% = {d['ship_budget_tokens']:,} weighted tokens",
                  f"  Session used: {used:,} weighted ({used/d['hard_budget_tokens']*100:.1f}% of session budget)" if d['hard_budget_tokens'] else f"  Session used: {used:,} weighted"]
    else:
        lines += ['  Token denominator: UNKNOWN — server exposes 5h utilization %, not raw quota tokens.',
                  '  Enforcement requires an explicit cap: set 5hTokenCap in config or use /limit ... <cap>.']
    lines += [f"  Raw tokens: {d['raw_tokens_used']:,} | Main: {d['main_weighted_tokens']:,} | Subagents: {d['subagent_weighted_tokens']:,} ({d['subagent_count']} transcripts)",
              f"  State: {state}",f"  Safety margin: {d.get('effective_safety_margin_tokens',d.get('safety_margin_tokens',DEFAULT_MARGIN)):,} weighted tokens"]
    if d.get('cap_source')=='plan_heuristic': lines.append('  WARNING: Anthropic does not publish a fixed raw-token 5h denominator; cap is an estimate. Server 5h % is authoritative.')
    return '\n'.join(lines)

def configure(d,budget,ship,cap=None,margin=None):
    if not (0<ship<budget<=100):raise ValueError('Require 0 < ship_percent < budget_percent <= 100.')
    d['enabled']=True;d['budget_percent']=budget;d['ship_percent']=ship
    if cap:d['estimated_5h_cap_tokens']=int(cap);d['cap_source']='explicit'
    else: detect_plan(d)
    if margin is not None:d['safety_margin_tokens']=max(0,int(margin))
    recalc(d);save(d);return d

def main():
    ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest='cmd',required=True)
    p=sp.add_parser('configure');p.add_argument('--session',required=True);p.add_argument('--budget-pct',type=float,required=True);p.add_argument('--ship-pct',type=float,required=True);p.add_argument('--cap',type=int);p.add_argument('--margin',type=int)
    for c in ['status','on','off','reset','check','reconcile']:
        p=sp.add_parser(c);p.add_argument('--session',required=True);p.add_argument('--transcript');p.add_argument('--subagent-transcript',action='append',default=[]);p.add_argument('--force-usage',action='store_true')
    sp.add_parser('self-test')
    sp.add_parser('ingest-statusline')
    a=ap.parse_args()
    if a.cmd=='ingest-statusline':
        try:
            payload=json.loads(sys.stdin.read())
            sid=payload.get('session_id')
            if sid:
                d=load(sid); fh=(payload.get('rate_limits') or {}).get('five_hour') or {}
                if fh.get('used_percentage') is not None:
                    d['five_hour_used_percent']=float(fh['used_percentage']); d['five_hour_reset']=fh.get('resets_at'); d['usage_source']='statusline'
                # The account meter is authoritative; no token counting is performed here.
                save(d)
        except Exception: pass
        return 0
    if a.cmd=='self-test':
        d=default_state('selftest');d=configure(d,50,45,1_000_000);assert d['hard_budget_tokens']==500_000;print('OK: v3 self-test passed (plan/cap math).');return 0
    d=load(a.session)
    if a.cmd=='configure':
        try:d=configure(d,a.budget_pct,a.ship_pct,a.cap,a.margin);refresh_usage(d,True);save(d);print(status(d));return 0
        except Exception as e:print(f'ERROR: {e}',file=sys.stderr);return 2
    if a.cmd in ('reconcile','check','status'):
        d=reconcile(d,a.transcript,a.subagent_transcript)
        if a.cmd=='status': d=refresh_usage(d,a.force_usage)
        detect_plan(d);recalc(d);save(d)
        if a.cmd=='check':
            print(json.dumps({'enabled':d['enabled'],'ship_mode':d['ship_mode'],'hard_stop':d['hard_stop'],'account_rate_limit_backstop':d.get('account_rate_limit_backstop',False),'weighted_tokens_used':d['weighted_tokens_used'],'raw_tokens_used':d['raw_tokens_used'],'main_weighted_tokens':d['main_weighted_tokens'],'subagent_weighted_tokens':d['subagent_weighted_tokens'],'subagent_count':d['subagent_count'],'hard_budget_tokens':d['hard_budget_tokens'],'ship_budget_tokens':d['ship_budget_tokens'],'five_hour_used_percent':d['five_hour_used_percent'],'five_hour_reset':d['five_hour_reset'],'plan_tier':d['plan_tier']},separators=(',',':')))
        else:print(status(d))
        return 0
    if a.cmd=='off':d['enabled']=False
    elif a.cmd=='on':d['enabled']=True;detect_plan(d);refresh_usage(d,True)
    elif a.cmd=='reset':
        for k in ['weighted_tokens_used','main_weighted_tokens','subagent_weighted_tokens','raw_tokens_used','main_raw_tokens','subagent_raw_tokens']:d[k]=0
        d['seen']={};d['offsets']={};d['ship_mode']=False;d['hard_stop']=False;d['warning_sent']=False
    recalc(d);save(d);print(status(d));return 0

if __name__=='__main__':sys.exit(main())
