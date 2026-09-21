from __future__ import annotations
import hashlib, json, os
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
from backend.state import now
from market_diary.storage import atomic_write_json

SENSITIVE=('key','token','secret','password','authorization','cookie')
def clean_url(url):
    p=urlsplit(url)
    return urlunsplit((p.scheme,p.netloc.split('@')[-1],p.path,urlencode([(k,'REDACTED' if any(s in k.lower() for s in SENSITIVE) else v) for k,v in parse_qsl(p.query)]),''))

def install_http_audit(directory):
    """Capture actual requests responses, including AkShare requests-based calls.
    Headers, request bodies and credentials are deliberately excluded.
    """
    import requests
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    original=getattr(requests.sessions.Session.send, "_audit_original", requests.sessions.Session.send)
    counter=[max([int(p.stem) for p in directory.glob("*.json") if p.stem.isdigit()] or [0])]
    def send(session,request,**kwargs):
        kwargs['timeout']=kwargs.get('timeout') or 12
        counter[0]+=1
        prefix=f'{counter[0]:05d}'
        record={'url':clean_url(request.url),'fetched_at':now(),'capture':'http_response','date_verified':False}
        try:
            response=original(session,request,**kwargs)
            body=response.content
            record.update(status=response.status_code,raw_hash=hashlib.sha256(body).hexdigest(),bytes=len(body))
            if len(body)<=12_000_000:(directory/f'{prefix}.bin').write_bytes(body)
            else:record['omitted']='响应超过 12MB，仅保留哈希'
            return response
        except Exception as exc:
            record['error']=type(exc).__name__;raise
        finally:atomic_write_json(directory/f'{prefix}.json',record)
    send._audit_original=original
    requests.sessions.Session.send=send
