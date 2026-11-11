"""Private, workspace-scoped problem memory and immutable paid-draw requests.

Only canonical IDs and evidence flags are downloaded. Server evidence is never
created from local answers. SQLite transactions protect snapshots and delivery
claims across restarts/processes; the practice transcript is read-only here.
"""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx


class ProblemMemoryError(ValueError):
    pass


def canonical_id(value):
    """Mirror ACM problem_sources.normalize without importing its server stack."""
    if not isinstance(value,str) or not 1<=len(value)<=200:raise ValueError('invalid problem ID')
    value=value.strip()
    if value.startswith('https://'):
        u=urlsplit(value)
        if u.netloc!=u.hostname or u.query or u.fragment:raise ValueError('invalid official URL')
        host=u.hostname
        if host=='atcoder.jp':
            match=re.fullmatch(r'/contests/([a-z0-9][a-z0-9-]{0,60})/tasks/([a-z0-9_]+?)/?',u.path)
            if not match:raise ValueError('invalid AtCoder URL')
            task=match[2]
            fixed={'practice_1':'abs','math_and_algorithm':'math-and-algorithm','tessoku_book':'tessoku-book'}
            contest=fixed.get(task) or fixed.get(task.rsplit('_',1)[0])
            if contest and contest!=match[1]:raise ValueError('invalid AtCoder collection')
            value='atcoder:'+task
        elif host=='www.luogu.com.cn':
            match=re.fullmatch(r'/problem/([A-Za-z0-9_]+?)/?',u.path)
            if not match:raise ValueError('invalid Luogu URL')
            value='luogu:'+match[1]
        elif host=='ac.nowcoder.com':
            match=re.fullmatch(r'/acm/contest/(\d+)/([A-Za-z]\d?)/?',u.path)
            if match:value=f'nowcoder:contest-{int(match[1])}-{match[2].upper()}'
            else:
                match=re.fullmatch(r'/acm/problem/(\d+)/?',u.path)
                if not match:raise ValueError('invalid Nowcoder URL')
                value='nowcoder:'+match[1]
        elif host in {'codeforces.com','www.codeforces.com'}:
            match=re.fullmatch(r'/(?:problemset/problem/(\d+)/([A-Za-z]\d?)|(?:contest|gym)/(\d+)/problem/([A-Za-z]\d?))/?',u.path)
            if not match:raise ValueError('invalid CF URL')
            value=f'codeforces:{match[1] or match[3]}-{match[2] or match[4]}'
        else:raise ValueError('unsupported official URL')
    formats=[
        (r'(?:codeforces:)?([1-9]\d{0,8})-([A-Za-z]\d?)',lambda m:f'codeforces:{int(m[1])}-{m[2].upper()}'),
        (r'atcoder:([a-z0-9]+(?:_[a-z0-9]+)+)',lambda m:'atcoder:'+m[1]),
        (r'(?:luogu:)?((?:P|B|CF|SP|UVA|AT|T|U)\d+[A-Za-z0-9_]*)',lambda m:'luogu:'+m[1].upper()),
        (r'nowcoder:([1-9]\d{0,11})',lambda m:'nowcoder:'+m[1]),
        (r'nowcoder:contest-([1-9]\d{0,11})-([A-Za-z]\d?)',lambda m:f'nowcoder:contest-{m[1]}-{m[2].upper()}'),
    ]
    for pattern,format_id in formats:
        match=re.fullmatch(pattern,value)
        if match:return format_id(match)
    raise ValueError('unsupported problem ID')


def _page(value):
    if not isinstance(value,dict):raise ProblemMemoryError('题号历史响应格式异常，未发起付费抽题。')
    scope=value.get('scope');revision=value.get('revision');items=value.get('items');cursor=value.get('next_after')
    if (not isinstance(scope,str) or not scope.strip() or len(scope)>200 or not isinstance(revision,str)
            or not re.fullmatch('[a-f0-9]{64}',revision) or not isinstance(items,list) or len(items)>1000):
        raise ProblemMemoryError('题号历史身份或分页格式异常，未发起付费抽题。')
    checked=[]
    for item in items:
        try:
            pid=item['problem_id']
            if canonical_id(pid)!=pid:raise ValueError('not canonical')
            flags={name:item[name] for name in ('attempted','solved','drawn','skipped')}
            if any(type(v) is not bool for v in flags.values()):raise ValueError('invalid flags')
        except (KeyError,TypeError,ValueError):
            raise ProblemMemoryError('题号历史包含不可核验记录，未发起付费抽题。') from None
        checked.append({'problem_id':pid,**flags})
    if cursor is not None and (not checked or not isinstance(cursor,str) or cursor!=checked[-1]['problem_id']):
        raise ProblemMemoryError('题号历史分页游标异常，未发起付费抽题。')
    return {'scope':scope,'revision':revision,'items':checked,'next_after':cursor}


class ProblemMemory:
    def __init__(self,root,*,origin='__ACM_ORIGIN__'):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.origin=origin.rstrip('/');self.path=self.root/'problem-memory.sqlite3'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600);os.fchmod(fd,0o600);os.close(fd)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS snapshots(origin TEXT,scope TEXT,revision TEXT,updated REAL,
                PRIMARY KEY(origin,scope));
              CREATE TABLE IF NOT EXISTS remote_items(origin TEXT,scope TEXT,pid TEXT,flags TEXT,
                PRIMARY KEY(origin,scope,pid));
              CREATE TABLE IF NOT EXISTS legacy_owner(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                origin TEXT NOT NULL,scope TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS delivered(origin TEXT,scope TEXT,pid TEXT,request_key TEXT,
                PRIMARY KEY(origin,scope,pid),UNIQUE(origin,request_key));
              CREATE TABLE IF NOT EXISTS requests(origin TEXT,request_key TEXT,scope TEXT,body TEXT,
                exclusions TEXT NOT NULL,parameters TEXT,state TEXT NOT NULL,
                PRIMARY KEY(origin,request_key));
            ''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        except sqlite3.Error:
            raise ProblemMemoryError('本地题号记忆暂不可读写，未自动重抽。') from None
        finally:db.close()

    async def _get(self,c,params):
        try:r=await c.get('/api/hermes/problem-memory',params=params)
        except httpx.HTTPError:
            raise ProblemMemoryError('题号历史同步连接中断，原缓存保留；未发起付费抽题。') from None
        if r.status_code==409:return None
        if r.status_code!=200:raise ProblemMemoryError('题号历史同步失败，原缓存保留；请检查登录与服务器版本。')
        if len(r.content)>2*1024*1024:raise ProblemMemoryError('题号历史分页过大，未发起付费抽题。')
        try:return _page(r.json())
        except ValueError as exc:
            if isinstance(exc,ProblemMemoryError):raise
            raise ProblemMemoryError('题号历史返回异常，原缓存保留。') from None

    async def sync(self,c):
        # Retry only this read-only pagination, once. Nothing paid is retried.
        for retry in range(2):
            scope=revision=after=None;items=[];changed=False
            while True:
                params={'limit':500}
                if after is not None:params.update(after=after,revision=revision)
                page=await self._get(c,params)
                if page is None:
                    changed=True;break
                if scope is not None and (scope!=page['scope'] or revision!=page['revision']):
                    raise ProblemMemoryError('同步期间题号历史身份或版本变化，原缓存保留。')
                scope,revision=page['scope'],page['revision']
                previous=after
                for item in page['items']:
                    if previous is not None and item['problem_id']<=previous:
                        raise ProblemMemoryError('题号历史分页顺序异常，原缓存保留。')
                    previous=item['problem_id'];items.append(item)
                if len(items)>200000:raise ProblemMemoryError('题号历史超过本机处理上限，未发起付费抽题。')
                after=page['next_after']
                if after is None:break
            if changed:
                if not retry:continue
                raise ProblemMemoryError('题号历史仍在变化，原缓存保留；请稍后重试。')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO snapshots VALUES(?,?,?,?) ON CONFLICT(origin,scope) DO UPDATE SET revision=excluded.revision,updated=excluded.updated',
                           (self.origin,scope,revision,time.time()))
                db.execute('DELETE FROM remote_items WHERE origin=? AND scope=?',(self.origin,scope))
                db.executemany('INSERT INTO remote_items VALUES(?,?,?,?)',[(self.origin,scope,item['problem_id'],json.dumps(item)) for item in items])
                db.execute('INSERT OR IGNORE INTO legacy_owner VALUES(1,?,?)',(self.origin,scope))
            return {'scope':scope,'revision':revision,'items':items,'next_after':None}

    def snapshot(self,scope):
        with self.db() as db:
            row=db.execute('SELECT * FROM snapshots WHERE origin=? AND scope=?',(self.origin,scope)).fetchone()
            if row is None:return None
            items=[json.loads(r[0]) for r in db.execute('SELECT flags FROM remote_items WHERE origin=? AND scope=? ORDER BY pid',(self.origin,scope))]
        return {'scope':scope,'revision':row['revision'],'items':items,'next_after':None}

    def backfill(self,scope):
        path=self.root/'practice.sqlite3'
        if not path.exists():return
        source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rounds'").fetchone():return
            rows=source.execute('SELECT id,result FROM rounds WHERE result IS NOT NULL').fetchall()
        finally:source.close()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO legacy_owner VALUES(1,?,?)',(self.origin,scope))
            owner=db.execute('SELECT origin,scope FROM legacy_owner WHERE singleton=1').fetchone()
            for request_key,raw in rows:
                try:
                    result=json.loads(raw);pid=canonical_id(result['problem']['id'])
                    tagged_scope=result.get('_memory_scope');tagged_origin=result.get('_memory_origin')
                except (ValueError,TypeError,KeyError):continue
                if tagged_origin is not None and tagged_origin!=self.origin:continue
                if tagged_scope is None:
                    if (owner['origin'],owner['scope'])!=(self.origin,scope):continue
                elif tagged_scope!=scope or (tagged_origin is None and owner['origin']!=self.origin):continue
                db.execute('INSERT OR IGNORE INTO delivered VALUES(?,?,?,?)',(self.origin,scope,pid,request_key))

    def exclusions(self,snapshot):
        self.backfill(snapshot['scope'])
        ids={item['problem_id'] for item in snapshot['items'] if any(item[k] for k in ('attempted','solved','drawn','skipped'))}
        with self.db() as db:
            ids.update(row[0] for row in db.execute('SELECT pid FROM delivered WHERE origin=? AND scope=?',(self.origin,snapshot['scope'])))
        if len(ids)>10000:raise ProblemMemoryError('待排除题号超过10000，未截断历史或发起付费抽题；请管理员检查记录。')
        return sorted(ids)

    def frozen(self,key):
        with self.db() as db:
            row=db.execute('SELECT * FROM requests WHERE origin=? AND request_key=?',(self.origin,key)).fetchone()
        if row is None:return None
        value=dict(row)
        for name in ('body','exclusions','parameters'):
            value[name]=json.loads(value[name]) if value[name] is not None else None
        return value

    def freeze(self,key,body,snapshot,*,parameters=None):
        ids=self.exclusions(snapshot);body=dict(body)
        if ids:body['exclude_ids']=ids
        else:body.pop('exclude_ids',None)
        encoded=json.dumps(body,ensure_ascii=False,sort_keys=True)
        params=json.dumps(parameters,ensure_ascii=False,sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM requests WHERE origin=? AND request_key=?',(self.origin,key)).fetchone()
            if old is None:
                db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',
                           (self.origin,key,snapshot['scope'],encoded,json.dumps(ids),params,'prepared'))
            elif old['scope']!=snapshot['scope'] or old['parameters']!=params:
                raise ProblemMemoryError('原抽题编号属于另一账号或参数，未重新付费抽题。')
        return self.frozen(key)

    def mark_post(self,key):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return bool(db.execute("UPDATE requests SET state='posted' WHERE origin=? AND request_key=? AND state='prepared'",(self.origin,key)).rowcount)

    def unaccepted(self,key):
        """A definite HTTP refusal permits retrying the SAME immutable request."""
        with self.db() as db:
            db.execute("UPDATE requests SET state='prepared' WHERE origin=? AND request_key=? AND state='posted'",(self.origin,key))

    async def recovery(self,c,key):
        page=await self._get(c,{'limit':1})
        if page is None:raise ProblemMemoryError('原任务的用户范围暂无法核对，请稍后恢复；未重新抽题。')
        old=self.frozen(key)
        if old is not None:
            if old['scope']!=page['scope']:raise ProblemMemoryError('当前登录用户与原抽题记录不符，未交付其他账号题目。')
            return old
        # Pre-upgrade accepted jobs have no client-side exclusion snapshot. The
        # authenticated server recovery owns the result; current remote memory
        # must not reject its own completed delivery as a new duplicate.
        self.backfill(page['scope'])
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,NULL,?,NULL,?)',
                       (self.origin,key,page['scope'],'[]','recovered'))
        context=self.frozen(key)
        if context['scope']!=page['scope']:raise ProblemMemoryError('原抽题编号已绑定另一工作区，未交付混用结果。')
        return context

    def claim(self,key,result,context):
        frozen=self.frozen(key)
        if frozen is None or frozen['scope']!=context['scope'] or frozen['exclusions']!=context['exclusions']:
            raise ProblemMemoryError('本次题号排除快照无法核对，未交付题目。')
        try:pid=canonical_id(result['problem']['id'])
        except (KeyError,TypeError,ValueError):raise ProblemMemoryError('返回题号无法规范核对，未交付题目。') from None
        if pid in context['exclusions']:raise ProblemMemoryError('服务器返回了本次已排除的题目，已拦截；不会自动付费重抽。')
        scope=context['scope']
        self.backfill(scope)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record=db.execute('SELECT scope,pid FROM delivered WHERE origin=? AND request_key=?',(self.origin,key)).fetchone()
            if record and (record['scope']!=scope or record['pid']!=pid):
                raise ProblemMemoryError('原抽题编号对应另一题目或账号，未交付变化结果。')
            owner=db.execute('SELECT request_key FROM delivered WHERE origin=? AND scope=? AND pid=?',(self.origin,scope,pid)).fetchone()
            if owner and owner['request_key']!=key:raise ProblemMemoryError('这道题已在本工作区抽取过，已拦截重复题；不会自动付费重抽。')
            db.execute('INSERT OR IGNORE INTO delivered VALUES(?,?,?,?)',(self.origin,scope,pid,key))
        result['_memory_scope']=scope;result['_memory_origin']=self.origin

    def counts(self,snapshot):
        ids=self.exclusions(snapshot)
        with self.db() as db:
            local=db.execute('SELECT COUNT(*) FROM delivered WHERE origin=? AND scope=?',(self.origin,snapshot['scope'])).fetchone()[0]
        return {'remote_count':len(snapshot['items']),'local_count':local,'exclude_count':len(ids)}
