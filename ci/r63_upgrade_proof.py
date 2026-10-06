"""Release-stage checks for the already independently inspected old-DB receipt."""
import hashlib,json,os,re
from pathlib import Path
CONTRACT=json.loads(Path(__file__).with_name('r63-upgrade-contract.json').read_text())

def validate_upgrade(proof, executable):
    c=CONTRACT
    if not isinstance(proof,dict) or set(proof)!=set(c['required_root_fields']):
        raise RuntimeError('Installed upgrade receipt fields are missing or unexpected')
    if any(proof.get(k) is not True for k in c['required_true_flags']) or any(proof.get(k) is not False for k in c['required_false_flags']):
        raise RuntimeError('Installed upgrade proof isolation or normal startup is incomplete')
    for key in ('contract','baseline_commit','legacy_schema_sha256','baseline_database_code_sha256','baseline_posting_code_sha256','chronology_clock'):
        if proof.get(key)!=c[key]:raise RuntimeError('Installed upgrade baseline mismatch: '+key)
    for key in ('nonce','manifest_sha256','input_database_sha256'):
        if not re.fullmatch('[a-f0-9]{64}',str(proof.get(key,''))):raise RuntimeError('Installed upgrade input binding missing: '+key)
    for key,value in (('network_attempts',0),('activity_attempts',0),('synthetic_open_admissions',3)):
        if type(proof.get(key)) is not int or proof[key]!=value:raise RuntimeError('Unexpected installed upgrade effect: '+key)
    for key in ('seed_pid','seed_completed_ns','opened_ns','seed_completed_perf_ns','opened_perf_ns'):
        if type(proof.get(key)) is not int or proof[key]<=0:raise RuntimeError('Installed upgrade process chronology missing')
    runtime=proof.get('runtime',{})
    if runtime.get('windows') is not True or runtime.get('frozen') is not True or type(runtime.get('pid')) is not int or runtime['pid']<=0 or runtime['pid']==proof['seed_pid'] or proof['opened_perf_ns']<=proof['seed_completed_perf_ns']:
        raise RuntimeError('Installed upgrade is not a separate frozen Windows process over old persisted data')
    executable=Path(executable).resolve(strict=True)
    if os.path.normcase(os.path.abspath(runtime.get('executable',''))) != os.path.normcase(str(executable)) or runtime.get('executable_sha256')!=hashlib.sha256(executable.read_bytes()).hexdigest():
        raise RuntimeError('Installed upgrade executable binding mismatch')
    module,bundle=Path(runtime.get('module_file','')),Path(runtime.get('bundle_root',''))
    if not module.is_absolute() or not bundle.is_absolute() or not module.is_relative_to(bundle) or module.parent.name!='app' or module.name not in {'nurture_cleanup_upgrade_selftest.py','nurture_cleanup_upgrade_selftest.pyc'}:
        raise RuntimeError('Installed upgrade module was not bundled')
    if proof.get('startup_sequence')!=c['startup_sequence'] or proof.get('persisted_state')!=c['persisted_state']:
        raise RuntimeError('Installed upgrade startup or external persisted-state oracle is incomplete')
    transition=proof.get('startup_action_transition',{})
    if transition.get('before')!='queued' or transition.get('after')!='paused' or type(transition.get('version_before')) is not int or transition['version_before']!=1 or type(transition.get('version_after')) is not int or transition['version_after']!=2 or not re.fullmatch('[a-f0-9]{64}',str(transition.get('after_sha256',''))):
        raise RuntimeError('Installed upgrade expected startup transition missing')
    if proof.get('queued_blockers_before_startup')!={'queued_collection':'collection','queued_studio':'studio','queued_monitor':'monitor','queued_action':'action','prepared_posting':'posting'}:
        raise RuntimeError('Installed upgrade queued/prepared blockers not proved')
    cases=proof.get('cases',{})
    if set(cases)!=set(c['case_outcomes']):raise RuntimeError('Installed upgrade cases are missing or unexpected')
    ids=[]
    for name,expected in c['case_outcomes'].items():
        case=cases[name]
        if not isinstance(case,dict) or set(case)!=set(c['case_fields']) or case.get('verified') is not True or case.get('hold_before') is not True:
            raise RuntimeError('Installed upgrade case fields invalid: '+name)
        if any(type(case.get(k)) is not type(v) or case[k]!=v for k,v in expected.items()):raise RuntimeError('Installed upgrade case result mismatch: '+name)
        if not case.get('job_id') or not case.get('profile_id') or not re.fullmatch('[a-f0-9]{64}',str(case.get('history_sha256',''))) or case.get('recovery_state')!=('reconciled_closed' if not case['hold_after'] else 'lease_lost'):
            raise RuntimeError('Installed upgrade historical case identity missing: '+name)
        ids.append(case['job_id'])
    if len(set(ids))!=18:raise RuntimeError('Installed upgrade cases share a historical job')
    admission=proof.get('admission',{})
    if set(admission)!=set(c['admission_cases']):raise RuntimeError('Installed upgrade admission coverage missing')
    new=[]
    for name,case in admission.items():
        if set(case)!=set(c['admission_fields']) or any(case[k] is not True for k in c['admission_fields'] if k!='new_job_id') or not case.get('new_job_id') or case['new_job_id'] in ids:raise RuntimeError('Installed upgrade new task/open evidence invalid: '+name)
        new.append(case['new_job_id'])
    if len(set(new))!=3:raise RuntimeError('Installed upgrade reused a new job')
    return proof

