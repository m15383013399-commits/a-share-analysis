"""Explicit, local-only import. Never changes the source or uploads research data."""
import sys,hashlib,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import state
from market_diary.storage import atomic_write_json

def main(source):
    state.init();source=Path(source).resolve();manifest=[]
    with state.lock():
        for folder in ['snapshots','forecasts','evaluations']:
            for p in sorted((source/'data'/folder).glob('*')):
                if p.suffix not in {'.json','.jsonl'} or not p.is_file():continue
                raw=p.read_bytes()
                if p.suffix=='.json':json.loads(raw)
                else:
                    for line in raw.splitlines():
                        if line:json.loads(line)
                target=state.RUNTIME/folder/p.name
                if target.exists() and target.read_bytes()!=raw:raise ValueError(f'目标已有不同文件，拒绝覆盖：{folder}/{p.name}')
                target.write_bytes(raw)
                manifest.append({'file':f'{folder}/{p.name}','sha256':hashlib.sha256(raw).hexdigest(),'origin':'legacy_local_import'})
        atomic_write_json(state.RUNTIME/'import-manifest.json',{'imported_at':state.now(),'files':manifest,'note':'历史原始文件保持不变；缺少原始响应的条目不补造'})
    print(f'Imported {len(manifest)} local files; no source data modified.')
if __name__=='__main__':main(sys.argv[1])
