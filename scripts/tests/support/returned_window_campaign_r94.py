"""200 stop/shutdown timing cases with real SQLite and owned renewal threads."""
from pathlib import Path
import argparse,asyncio,json,sys,time,traceback
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]
from test_returned_window_release_r73 import ReturnedWindowRuntimeTests

async def scenario(index):
    test = ReturnedWindowRuntimeTests()
    await test.asyncSetUp()
    try:
        await test.check_heartbeat_connection_drain(shutdown=bool(index % 2),
            repeated_cancels=(index // 2) % 4, hold_seconds=.001 + (index // 8) * .001)
    finally:
        await test.asyncTearDown()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();failures=0
    with (args.output/'cases.jsonl').open('w',encoding='utf-8') as stream:
        for index in range(200):
            row={'id':index,'shutdown':bool(index%2),'repeated_cancels':(index//2)%4,
                 'hold_seconds':.001+(index//8)*.001}
            try:
                asyncio.run(scenario(index));row['status']='passed'
            except Exception:
                failures+=1;row.update(status='failed',error=traceback.format_exc())
            stream.write(json.dumps(row)+'\n');stream.flush()
            if (index+1)%25==0:print(f'{index+1}/200; failures={failures}',flush=True)
    result={'scenarios':200,'passed':200-failures,'failed':failures,
            'real_sqlite_connections':True,'windows_share_locks_tested':False,
            'seconds':round(time.monotonic()-start,3)}
    (args.output/'summary.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True);return bool(failures)

if __name__=='__main__':raise SystemExit(main())
