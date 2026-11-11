"""Private, persistent per-bot/chat/player practice state; no judge execution."""
from acm_roll_commands import parse_roll, select_score
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from weakref import WeakValueDictionary
from practice_settings import grading_options
from practice_text import problem_pages, sample_text

ANSWER_TIMEOUT = 30 * 60

class PracticeError(Exception):
    pass

@dataclass
class Reply:
    text: str
    images: list = field(default_factory=list)
    delivery: dict | None = None

STAGES={'queued':'等待处理','validating':'检查配置','preparing_image':'检查图片组件',
 'checking_sessions':'检查平台登录','syncing_submissions':'同步已做题目',
 'syncing_catalog':'同步 Codeforces 题库','selecting':'随机选题','reading_statement':'读取题面',
 'generating_report':'生成题目说明和私有报告','revalidating':'核对题目资格',
 'rendering_image':'生成图片','completed':'已完成','failed':'未完成','interrupted':'已中断'}

REASONS = {
 'missing_argument':'关键结论缺少必要论证', 'contradiction':'提交内容存在前后矛盾',
 'constraint':'尚未说明能否满足题目约束', 'edge_case':'边界情况尚未覆盖',
 'complexity':'复杂度分析缺失或与描述不符', 'implementation':'实现细节与描述的思路不一致',
 'incomplete':'关键步骤描述不完整', 'unverifiable':'目前的信息不足以判断正确性',
}

def validate_grade(value, answer):
    if not isinstance(value,dict) or type(value.get('passed')) is not bool:
        raise PracticeError('判题结果格式异常，本次不计通过，请稍后重新 /submit。')
    checks=value.get('checks')
    required={'reasoning','constraints','complexity','edge_cases','implementation'}
    issues=value.get('issues')
    if not isinstance(checks,dict) or set(checks)!=required or any(type(v) is not bool for v in checks.values()):
        raise PracticeError('判题结果不完整，本次不计通过，请稍后重新 /submit。')
    if not isinstance(issues,list) or len(issues)>3:
        raise PracticeError('判题结果格式异常，请稍后重新 /submit。')
    blocking=[]
    for issue in issues:
        if not isinstance(issue,dict) or issue.get('kind') not in {'reasoning','implementation'} or issue.get('reason') not in REASONS or issue.get('severity') not in {'blocking','advice'}:
            raise PracticeError('判题结果格式异常，请稍后重新 /submit。')
        quote=issue.get('quote','')
        if not isinstance(quote,str) or (quote and (len(quote)>100 or quote not in answer)):
            raise PracticeError('判题引用无法核对，请稍后重新 /submit。')
        for name,limit in [('detail',160),('question',180)]:
            text=issue.get(name,'')
            if not isinstance(text,str) or len(text)>limit:
                raise PracticeError('判题反馈格式异常，请稍后重新 /submit。')
            if '```' in text:
                raise PracticeError('判题反馈包含代码块，请稍后重新 /submit。')
        if issue['severity']=='blocking':
            if not (issue.get('detail','').strip() or issue.get('question','').strip()):
                raise PracticeError('判题反馈缺少具体问题，请稍后重新 /submit。')
            blocking.append(issue)
    if value['passed'] and (not checks['reasoning'] or not checks['constraints'] or blocking):
        raise PracticeError('判题结论不一致，本次不计通过，请稍后重新 /submit。')
    if not value['passed'] and not blocking:
        raise PracticeError('判题未指出阻止通过的问题，请稍后重新 /submit。')
    safe=[]
    for issue in (blocking if not value['passed'] else issues)[:2]:
        label='思路' if issue['kind']=='reasoning' else '实现'
        quote=issue.get('quote','')
        location=('你写的“'+quote+'”：') if quote else ''
        if value.get('_feedback_reviewed') is True:
            detail=issue.get('detail','').strip()
            question=issue.get('question','').strip()
            text=label+'：'+location+detail
            if question:text+='\n追问：'+question
        else:
            text=label+'：'+location+REASONS[issue['reason']]+'，请补充这一步的具体操作或判断依据。'
        safe.append(text)
    return value['passed'], '\n'.join(safe)

class Store:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.root.chmod(0o700)
        self.path=self.root/'practice.sqlite3'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
              score INTEGER NOT NULL, topic TEXT, created REAL NOT NULL, updated REAL NOT NULL, result TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_scope ON rounds(scope) WHERE status IN ('drawing','active','grading');
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, scope TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, round_id TEXT NOT NULL, answer TEXT NOT NULL,
              status TEXT NOT NULL, feedback TEXT, created REAL NOT NULL);
            ''')
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS seats(round_id TEXT NOT NULL, user TEXT NOT NULL,
              status TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(round_id,user));''')
            db.executescript('''CREATE TABLE IF NOT EXISTS draw_controls(round_id TEXT PRIMARY KEY,
              owner TEXT NOT NULL, post_attempted INTEGER NOT NULL DEFAULT 0,
              stop_requested INTEGER NOT NULL DEFAULT 0, job_id TEXT);''')
            # attempts 以前没有用户维度；补一列，(round_id,user) 才是唯一答案记录。
            try:db.execute("ALTER TABLE attempts ADD COLUMN user TEXT DEFAULT ''")
            except sqlite3.OperationalError:pass
            db.execute('CREATE TABLE IF NOT EXISTS round_timers(round_id TEXT PRIMARY KEY, deadline REAL NOT NULL, expired INTEGER NOT NULL DEFAULT 0)')
            # Legacy rounds have no delivery timestamp; keep their last known
            # activity as the migration baseline, never reset it on restart.
            db.execute("INSERT OR IGNORE INTO round_timers(round_id,deadline) SELECT id,updated+? FROM rounds WHERE status IN ('active','grading')",(ANSWER_TIMEOUT,))
            # Upgrade open fixed-duration timers using actual submitted times,
            # never the restart time. Closed rounds remain closed.
            db.execute("UPDATE round_timers SET deadline=MAX(deadline,COALESCE((SELECT MAX(created)+? FROM attempts WHERE round_id=round_timers.round_id),deadline)) WHERE expired=0 AND round_id IN (SELECT id FROM rounds WHERE status IN ('active','grading'))",(ANSWER_TIMEOUT,))
    @staticmethod
    def _expire(db,now):
        rows=db.execute("SELECT r.id FROM rounds r JOIN round_timers t ON t.round_id=r.id WHERE r.status IN ('active','grading') AND t.deadline<=?",(now,)).fetchall()
        for row in rows:
            rid=row['id']
            db.execute("UPDATE rounds SET status='givenup',updated=? WHERE id=?",(now,rid))
            db.execute("UPDATE seats SET status='givenup',updated=? WHERE round_id=? AND status IN ('active','grading')",(now,rid))
            db.execute("UPDATE attempts SET status='cancelled',feedback='auto_giveup_timeout' WHERE round_id=? AND status='grading'",(rid,))
            db.execute('UPDATE round_timers SET expired=1 WHERE round_id=?',(rid,))
        return len(rows)
    def expire(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._expire(db,time.time())
    def timed_out(self,rid):
        with self.db() as db:
            row=db.execute('SELECT expired FROM round_timers WHERE round_id=?',(rid,)).fetchone()
        return bool(row and row['expired'])
    def begin_submission(self,rid,user,event,answer):
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db,now)
            row=db.execute('SELECT status FROM rounds WHERE id=?',(rid,)).fetchone()
            if not row or row['status'] not in {'active','grading'}:return False
            n=db.execute("UPDATE seats SET status='grading',updated=? WHERE round_id=? AND user=? AND status='active'",(now,rid,str(user))).rowcount
            if not n:return False
            db.execute("INSERT INTO attempts(id,round_id,answer,status,feedback,created,user) VALUES(?,?,?,'grading',NULL,?,?)",(event,rid,answer,now,str(user)))
            # Only an accepted /submit refreshes the shared round's timer.
            db.execute('INSERT INTO round_timers(round_id,deadline) VALUES(?,?) ON CONFLICT(round_id) DO UPDATE SET deadline=MAX(round_timers.deadline,excluded.deadline)',(rid,now+ANSWER_TIMEOUT))
            return True
    def finish_grade(self,rid,user,event,passed,feedback):
        # Expiry and a late PASS compete in one transaction. The losing result
        # cannot reopen an expired round or overwrite its cancellation record.
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db,now)
            row=db.execute('SELECT status FROM rounds WHERE id=?',(rid,)).fetchone()
            n=0
            if row and row['status'] in {'active','grading'}:
                n=db.execute("UPDATE seats SET status=?,updated=? WHERE round_id=? AND user=? AND status='grading'",('passed' if passed else 'active',now,rid,str(user))).rowcount
            if not n:
                db.execute("UPDATE attempts SET status='cancelled',feedback='round_closed' WHERE id=? AND status='grading'",(event,))
                return False
            if passed:db.execute("UPDATE rounds SET status='passed',updated=? WHERE id=?",(now,rid))
            db.execute("UPDATE attempts SET status=?,feedback=? WHERE id=? AND status='grading'",('passed' if passed else 'retry',feedback,event))
            return True
    def seat(self,rid,user):
        with self.db() as db:
            row=db.execute('SELECT * FROM seats WHERE round_id=? AND user=?',(rid,str(user))).fetchone()
        return dict(row) if row else None
    def seat_set(self,rid,user,status,only_from=None):
        user=str(user);now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if only_from:
                n=db.execute('UPDATE seats SET status=?,updated=? WHERE round_id=? AND user=? AND status=?',(status,now,rid,user,only_from)).rowcount
                if not n:return None
            else:
                db.execute('INSERT INTO seats(round_id,user,status,updated) VALUES(?,?,?,?) ON CONFLICT(round_id,user) DO UPDATE SET status=excluded.status,updated=excluded.updated',(rid,user,status,now))
            row=db.execute('SELECT * FROM seats WHERE round_id=? AND user=?',(rid,user)).fetchone()
        return dict(row) if row else None
    def join(self,rid,user):
        seat=self.seat(rid,user)
        return seat or self.seat_set(rid,user,'active')
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        return db
    def row(self,scope):
        with self.db() as db:
            row=db.execute('SELECT * FROM rounds WHERE scope=? ORDER BY created DESC,rowid DESC LIMIT 1',(scope,)).fetchone()
        return dict(row) if row else None
    def by_id(self,rid):
        with self.db() as db:
            row=db.execute('SELECT * FROM rounds WHERE id=?',(rid,)).fetchone()
        return dict(row) if row else None
    def cache_brief(self,rid,text):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,result FROM rounds WHERE id=?',(rid,)).fetchone()
            if not row or row['status'] not in {'passed','givenup'}:return
            result=json.loads(row['result'] or '{}')
            result['_solution_brief']=text
            db.execute('UPDATE rounds SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),rid))
    def claim(self,scope,event):
        with self.db() as db:
            db.execute('DELETE FROM events WHERE created<?',(time.time()-7*86400,))
            try:db.execute('INSERT INTO events VALUES(?,?,?)',(event,scope,time.time()))
            except sqlite3.IntegrityError:return False
        return True
    def start(self,scope,score,topic,owner=''):
        now=time.time();rid=uuid.uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM rounds WHERE scope=? AND status IN ('drawing','active','grading')",(scope,)).fetchone()
            if row:return dict(row),False
            if db.execute("SELECT count(*) FROM rounds WHERE status='drawing'").fetchone()[0]>=3:
                raise PracticeError('当前已有 3 道题正在生成，请稍后再抽。')
            if db.execute('SELECT count(*) FROM rounds WHERE scope=? AND created>?',(scope,now-3600)).fetchone()[0]>=6:
                raise PracticeError('你在本聊天一小时内已抽题 6 次，请稍后继续。')
            if db.execute('SELECT count(*) FROM rounds WHERE created>?',(now-3600,)).fetchone()[0]>=24:
                raise PracticeError('刷题服务本小时的抽题次数已达上限，请稍后继续。')
            db.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?,NULL)',(rid,scope,'drawing',score,topic,now,now))
            db.execute('INSERT INTO draw_controls(round_id,owner) VALUES(?,?)',(rid,str(owner)))
        return self.row(scope),True
    def control(self,rid):
        with self.db() as db:
            row=db.execute('SELECT * FROM draw_controls WHERE round_id=?',(rid,)).fetchone()
        # Pre-upgrade uncertain requests may already have reached the server.
        return dict(row) if row else dict(owner='',post_attempted=1,stop_requested=0,job_id=None)
    def request_stop(self,rid):
        with self.db() as db:
            db.execute('INSERT INTO draw_controls(round_id,owner,post_attempted,stop_requested) VALUES(?,\'\',1,1) ON CONFLICT(round_id) DO UPDATE SET stop_requested=1',(rid,))
    def authorize_post(self,rid):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM rounds WHERE id=?',(rid,)).fetchone()
            control=db.execute('SELECT stop_requested FROM draw_controls WHERE round_id=?',(rid,)).fetchone()
            if not row or row['status']!='drawing' or control and control['stop_requested']:return False
            db.execute('UPDATE draw_controls SET post_attempted=1 WHERE round_id=?',(rid,))
            return True
    def set_job(self,rid,jid):
        with self.db() as db:
            db.execute('UPDATE draw_controls SET job_id=? WHERE round_id=?',(jid,rid))
    def finish_stop(self,rid):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            n=db.execute("UPDATE rounds SET status='givenup',updated=? WHERE id=? AND status='drawing'",(time.time(),rid)).rowcount
            if n:db.execute("UPDATE seats SET status='givenup',updated=? WHERE round_id=? AND status='active'",(time.time(),rid))
    def history(self,scope,page):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT id,score,status FROM rounds WHERE scope=? ORDER BY created DESC,rowid DESC LIMIT 5 OFFSET ?',(scope,(page-1)*5))]
    def change(self,rid,old,new,result=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            now=time.time()
            if result is None:
                n=db.execute('UPDATE rounds SET status=?,updated=? WHERE id=? AND status=?',(new,now,rid,old)).rowcount
            else:
                n=db.execute('UPDATE rounds SET status=?,updated=?,result=? WHERE id=? AND status=?',(new,now,json.dumps(result,ensure_ascii=False),rid,old)).rowcount
            if n and old=='drawing' and new=='active':
                db.execute('INSERT OR IGNORE INTO round_timers(round_id,deadline) VALUES(?,?)',(rid,now+ANSWER_TIMEOUT))
        return bool(n)

class Game:
    def __init__(self,store,api,grader,*,summarizer=None):
        self.store,self.api,self.grader=store,api,grader
        self.summarizer=summarizer
        self._brief_locks=WeakValueDictionary()
        self.drawing={}
        self.sem=asyncio.Semaphore(3)
    @staticmethod
    def scope(bot,chat,user):
        return json.dumps([str(bot),str(chat),str(user)],separators=(',',':'))
    def solution(self,row,page=1):
        result=json.loads(row.get('result') or '{}');report=result.get('report',{})
        body='解题思路（AI 静态分析，未运行代码）：\n'+report.get('summary','')+'\n'+'\n'.join(f'{i+1}. {v}' for i,v in enumerate(report.get('approach',[])))
        pages=[body[i:i+800] for i in range(0,len(body),800)]
        if not 1<=page<=len(pages):return Reply('页码范围：1～'+str(len(pages)))
        return Reply(f'[{row["id"][:8]} 思路 {page}/{len(pages)}]\n'+pages[page-1]+(f'\n下一页：/solution {page+1}' if page<len(pages) else ''))
    async def solution_brief(self,row):
        rid=row['id']
        lock=self._brief_locks.get(rid)
        if lock is None:lock=self._brief_locks[rid]=asyncio.Lock()
        async with lock:
            current=self.store.by_id(rid)
            if not current or current['status'] not in {'passed','givenup'}:
                return Reply('本题尚未通过或放弃，暂不提供解法。')
            result=json.loads(current.get('result') or '{}')
            text=result.get('_solution_brief')
            if not isinstance(text,str) or not text.strip() or len(text)>220:
                try:
                    if self.summarizer is None:
                        text=result.get('report',{}).get('summary','')
                    else:
                        text=await asyncio.wait_for(self.summarizer(result),42)
                    if not isinstance(text,str) or not text.strip() or len(text)>220 or '```' in text:
                        raise ValueError('invalid brief')
                    text=' '.join(text.split())
                except Exception:
                    # Optional wording must never turn an already-closed PASS
                    # or giveup into a grading/service failure.
                    text='解法概要暂未生成，可以继续追问具体步骤。'
                self.store.cache_brief(rid,text)
            return Reply(f'[{rid[:8]}] 解法：{text}\n想看细节可以继续问。')
    def problem(self,row,page=1,*,text=False):
        result=json.loads(row.get('result') or '{}')
        if not text and result.get('_images'):
            paths=result['_images']
            image_page=page-1
            if not 0<=image_page<len(paths):return Reply('图片页码范围：1～'+str(len(paths)))
            path=Path(paths[image_page])
            expected=self.store.root/'images'/row['id']
            if result.get('_cached_until',0)>time.time() and path.is_file() and path.parent.resolve()==expected.resolve() and not path.is_symlink():
                return Reply(f'[{row["id"][:8]} 题面图片 {page}/{len(paths)}]\n'+
                    (f'下一页：/problem {page+1}\n' if page<len(paths) else '')+
                    '/submit 思路及关键实现 · /sample [组数] · /giveup\n30 分钟未提交自动放弃，每次 /submit 重新计时。', [str(path)])
            return Reply('题面图片已过期，作答与报告仍保留；/problem text 看文字，/sample 看样例，不会重新收费生成。')
        if not text and result.get('format')=='image':
            return Reply(result.get('_image_notice') or '图片暂不可用；/problem text 看文字，/sample 看样例，不会重新收费生成。')
        try:pages=problem_pages(json.loads(row.get('result') or '{}'))
        except (ValueError,TypeError):return Reply('本题中文说明暂不可读；原作答记录保留，可 /submit 或 /giveup。')
        if not 1<=page<=len(pages):return Reply('题面页码范围：1～'+str(len(pages)))
        return Reply(f'[{row["id"][:8]} 题面 {page}/{len(pages)}]\n'+pages[page-1]+
            (f'\n下一页：/problem text {page+1}' if page<len(pages) else '')+
            '\n/submit 思路及关键实现 · /sample 看一组样例 · /giveup 查看思路\n30 分钟未提交自动放弃，每次 /submit 重新计时。')
    async def cancel_draw(self,row):
        rid=row['id'];self.store.request_stop(rid)
        control=self.store.control(rid)
        if not control['post_attempted']:
            self.store.finish_stop(rid)
            return Reply(f'[{rid[:8]}] 已停止，未提交收费抽题。')
        job=await self.api.cancel_request(rid)
        if job is None:
            return Reply(f'[{rid[:8]}] 停止请求已记录，服务器尚未找到原任务；请稍后 /progress 或 /cancel。不会另行抽题。')
        state=(job.get('control') or {}).get('stop_state')
        if state=='stopped' or job.get('status') in {'completed','failed'} or job.get('status')=='interrupted' and state!='stopping':
            self.store.finish_stop(rid)
            return Reply(f'[{rid[:8]}] 抽题流程已停止。已发出的上游工作或费用不保证撤销；可用新 /roll 抽题。')
        return Reply(f'[{rid[:8]}] 已请求停止，正在清理；用 /progress 查询，不会自动重新抽题。')
    async def progress(self,row):
        if not row:return Reply('当前没有题目。')
        if self.store.timed_out(row['id']):return Reply('本题已连续 30 分钟未提交，已自动放弃；/solution 看思路，或 /roll 抽新题。')
        control=self.store.control(row['id'])
        if row['status']=='drawing' and control['stop_requested']:return await self.cancel_draw(row)
        job=await self.api.recover(row['id'])
        if not job:return Reply(f'[{row["id"][:8]}] 服务器尚未找到原请求；/roll 只恢复这个编号。')
        stage=(job.get('control') or {}).get('stage',job.get('status'))
        return Reply(f'[{row["id"][:8]}] '+STAGES.get(stage,'状态待确认')+'。'+
            ('用 /roll 恢复本题交付。' if job.get('status')=='completed' and row['status']=='drawing' else ''))
    async def tasks(self,shared,page):
        rows=self.store.history(shared,page)
        if not rows:return Reply('本聊天这一页没有抽题记录。')
        listing=await self.api.list_jobs()
        remote={j.get('id'):j for j in listing.get('items',[]) if isinstance(j,dict)}
        lines=[]
        for row in rows:
            jid=self.store.control(row['id'])['job_id'];job=remote.get(jid)
            if not job:job=await self.api.recover(row['id'])
            stage=(job.get('control') or {}).get('stage',job.get('status')) if job else None
            lines.append(f'[{row["id"][:8]}] {row["score"]} · '+STAGES.get(stage,'本地状态：'+row['status']))
        return Reply('本聊天抽题记录（第 '+str(page)+' 页）：\n'+'\n'.join(lines)+'\n下一页：/tasks '+str(page+1))
    async def handle(self,scope,event,text,notice=None,shared=None,user='',admin=False):
        shared=shared or scope;user=str(user)
        fields=text.strip().split(None,1);cmd=fields[0].lower();arg=fields[1].strip() if len(fields)>1 else ''
        if not self.store.claim(scope,event):return None
        self.store.expire()
        row=self.store.row(shared)
        if cmd=='/progress':return await self.progress(row)
        if cmd=='/cancel':
            if not row or row['status']!='drawing':return Reply('当前没有正在生成的题目；放弃作答用 /giveup。')
            if not admin and self.store.control(row['id'])['owner']!=user:return Reply('仅本次抽题发起人或管理员可停止群共用题目的生成。')
            return await self.cancel_draw(row)
        if cmd=='/tasks':
            if arg and (not arg.isdecimal() or len(arg)>4 or int(arg)<1):return Reply('用法：/tasks [页码]')
            return await self.tasks(shared,int(arg or '1'))
        if cmd=='/candidates':
            parts=arg.split(None,1)
            if not parts or len(parts[0])>4 or not parts[0].isdecimal() or not 800<=int(parts[0])<=3500 or len(arg)>90:return Reply('用法：/candidates 1200 [题型]')
            value=await self.api.candidates(int(parts[0]),parts[1] if len(parts)>1 else None)
            items=value.get('items',[])
            clean=lambda s:' '.join(str(s).split())[:90]
            return Reply('缓存候选预览（不调用 AI，不锁定题目）：\n'+'\n'.join(clean(p.get('id',''))+' · '+clean(p.get('title','')) for p in items[:5])+f'\n本次缓存共 {len(items)} 项，最多展示前 5 项。')
        if cmd=='/practice':
            if arg=='ready':
                state=await self.api.readiness()
                return Reply('默认模型配置：'+('已就绪' if state['ai_ready'] else '未就绪')+'；图片接口：'+('支持' if state['image'] else '未支持')+'。图片组件会在收费 AI 调用前检查；此处不检测上游或启动浏览器。')
            if arg=='stop-all':
                if not admin:return Reply('仅管理员可急停 ACM 账号全部抽题任务；本题生成停止用 /cancel。')
                with self.store.db() as db:rows=[dict(r) for r in db.execute("SELECT * FROM rounds WHERE status='drawing'")]
                for r in rows:self.store.request_stop(r['id'])
                await self.api.stop_all()
                for r in rows:await self.cancel_draw(r)
                return Reply('已向 ACM 账号全部抽题任务请求停止；用 /progress 确认本题清理状态。已发出的工作或费用不保证撤销。')
            state={'drawing':'抽题生成中','active':'等待作答','grading':'判题中','passed':'已关闭（有人通过）','givenup':'已关闭（有人放弃）','error':'生成失败'}
            mine=''
            if row:
                seat=self.store.seat(row['id'],user)
                mine='；你：'+state.get(seat['status'],seat['status']) if seat else '；你还没参与'
            current=state.get(row['status'],row['status']) if row else ''
            if row and self.store.timed_out(row['id']):current='已关闭（30 分钟自动放弃）'
            return Reply('/roll [分数] [题型表达式]（图片，&交集、|并集） · /problem [页码|text 页码] · /sample [组数]\n/submit 思路及实现 · /giveup · /solution [页码]\n/progress · /cancel · /tasks [页码] · /candidates 分数 [题型] · /practice ready\n连续 30 分钟未提交自动放弃；每次 /submit 重新计时，首次从题面就绪起算。\n'+(f'当前 [{row["id"][:8]}]：'+current+mine if row else '当前没有题目。'))
        if cmd=='/problem':
            if not row or not row.get('result'):return Reply('当前没有可读题面，请先 /roll 分数 抽题。')
            parts=arg.split();as_text=bool(parts and parts[0].lower()=='text')
            if as_text:parts=parts[1:]
            if len(parts)>1 or parts and (not parts[0].isdecimal() or len(parts[0])>6):return Reply('用法：/problem [页码] 或 /problem text [页码]')
            return self.problem(row,int(parts[0]) if parts else 1,text=as_text)
        if cmd=='/sample':
            if not row or not row.get('result'):return Reply('当前没有题目，请先 /roll 分数 抽题。')
            if arg and (not arg.isdecimal() or len(arg)>3):return Reply('用法：/sample [第几组]')
            try:text=sample_text(json.loads(row.get('result') or '{}'),int(arg or '1')-1)
            except (ValueError,TypeError):text=None
            return Reply(text or '这题没有附带样例，按题面直接作答即可。')
        if cmd=='/solution':
            if not row or row['status'] not in {'active','grading','passed','givenup'}:return Reply('当前没有题目，请先 /roll 分数 抽题。')
            seat=self.store.seat(row['id'],user)
            if row['status'] not in {'passed','givenup'} and (not seat or seat['status'] not in {'passed','givenup'}):return Reply('通过或 /giveup 之后才能看本题思路。')
            if arg and (not arg.isdecimal() or len(arg)>6):return Reply('用法：/solution [页码]')
            return self.solution(row,int(arg)) if arg else await self.solution_brief(row)
        if cmd=='/giveup':
            if row and row['status']=='drawing':
                if not admin and self.store.control(row['id'])['owner']!=user:return Reply('题目仍在生成，只有发起人或管理员可 /cancel。')
                return await self.cancel_draw(row)
            if not row or row['status'] not in {'active','grading'}:return Reply('没有正在进行的题；要抽题就 /roll。')
            seat=self.store.seat(row['id'],user)
            if not seat or seat['status'] in {'passed','givenup'}:return Reply('你在这道题上没有进行中的作答。')
            if not self.store.seat_set(row['id'],user,'givenup',only_from=seat['status']):return Reply('状态刚刚变化，请用 /practice 查看。')
            self.store.change(row['id'],row['status'],'givenup')
            if row['result']:
                reply=await self.solution_brief(row);reply.text='已放弃本题。\n'+reply.text;return reply
            return Reply('已放弃本题并关闭。要新题就 /roll。')
        if cmd=='/roll':
            if row and row['status'] in {'active','grading'}:
                self.store.join(row['id'],user)
                reply=self.problem(row)
                reply.delivery=json.loads(row.get('result') or '{}').get('_receipt')
                reply.text='这道题是群里共用的一题，你也算参与了。\n'+reply.text
                return reply
            if row and row['status']=='drawing':
                if self.store.control(row['id'])['stop_requested']:return await self.cancel_draw(row)
                if row['id'] in self.drawing:return Reply(f'[{row["id"][:8]}] 仍在生成，不会重复抽题。可用 /giveup 取消本地作答。')
                if notice:await notice(f'[{row["id"][:8]}] 正在恢复原抽题任务，请稍候。')
                return await self.draw(row,accept_text=arg.casefold()=='text')
            # ACM roll expression adapter v1
            try:
                score,topic=parse_roll(arg)
            except ValueError as error:
                return Reply(str(error))
            # Validate authentication and module readiness before reserving or paying.
            await self.api.ready()
            if score is None:
                previous=row.get('score') if row else None
                try:
                    score=select_score(previous)
                except ValueError:
                    preferences=await self.api.training_preferences()
                    try:
                        score=select_score(previous,preferences)
                    except ValueError as error:
                        return Reply(str(error))
            row,new=self.store.start(shared,score,topic,owner=user)
            if not new:return Reply('已有正在进行的题目，请用 /practice 查看。')
            self.store.join(row['id'],user)
            if notice:await notice(f'[{row["id"][:8]}] 已开始生成中文题面，可能需要几分钟；可用 /giveup 取消本地作答。')
            return await self.draw(row)
        if cmd=='/submit':
            if not arg:return Reply('用法：/submit 你的思路、复杂度和关键实现步骤（也可附代码）。')
            if len(arg)>20000:return Reply('提交过长，请控制在 20000 字以内。')
            if row and self.store.timed_out(row['id']):return Reply('本题已连续 30 分钟未提交，已自动放弃；/solution 看思路，或 /roll 抽新题。')
            if not row or row['status'] not in {'active','grading'}:return Reply('这道题已经关闭了（有人通过或放弃），要再来一题就 /roll。')
            seat=self.store.join(row['id'],user)
            if seat['status'] in {'passed','givenup'}:return Reply('你在这道题上已经结束了，等群里下一题再答吧。')
            try:limits=grading_options()
            except (OSError,ValueError):raise PracticeError('判题配置暂不可用，题目仍保留；请管理员检查本机配置。') from None
            if seat['status']=='grading' and time.time()-seat['updated']>limits['operation_timeout']+60:
                seat=self.store.seat_set(row['id'],user,'active')
            with self.store.db() as db:
                recent=db.execute('SELECT count(*) FROM attempts WHERE user=? AND created>?',(user,time.time()-600)).fetchone()[0]
            if recent>=10:return Reply('你 10 分钟内已提交 10 次，请稍后继续。')
            if seat['status']=='grading':return Reply('你上一份思路还在分析，请稍候；也可 /giveup。')
            if not self.store.begin_submission(row['id'],user,event,arg):return Reply('本题已结束或上一份思路还在分析，请用 /practice 查看。')
            try:
                async def evaluate():
                    async with self.sem:
                        self.store.expire()
                        if self.store.by_id(row['id'])['status'] not in {'active','grading'}:
                            raise PracticeError('本题已结束，未继续调用判题。用 /roll 抽新题。')
                        return await self.grader(json.loads(row['result']),arg)
                grade=await asyncio.wait_for(evaluate(),limits['operation_timeout'])
                passed,feedback=validate_grade(grade,arg)
                if not self.store.finish_grade(row['id'],user,event,passed,feedback):return Reply('本次作答已取消或超时，判题结果未套用到其他题目。')
                if passed:
                    reply=await self.solution_brief(row);reply.text='恭喜PASS哈 🎉（AI 思路评估通过）\n'+reply.text;return reply
                return Reply(f'[{row["id"][:8]}] 暂未通过：\n'+feedback+'\n可修改后继续 /submit，或 /giveup。')
            except asyncio.TimeoutError:
                self.store.seat_set(row['id'],user,'active',only_from='grading')
                with self.store.db() as db:db.execute("UPDATE attempts SET status='error',feedback='grading_timeout' WHERE id=? AND status='grading'",(event,))
                raise PracticeError('判题等待超时，题目仍保留；请稍后重新 /submit。') from None
            except BaseException as exc:
                self.store.seat_set(row['id'],user,'active',only_from='grading')
                feedback=str(exc) if isinstance(exc,PracticeError) else 'grading_interrupted_or_internal_error'
                with self.store.db() as db:db.execute("UPDATE attempts SET status='error',feedback=? WHERE id=? AND status='grading'",(feedback,event))
                raise
        return Reply('用 /practice 查看刷题指令。')
    async def draw(self,row,*,accept_text=False):
        rid=row['id'];self.drawing[rid]=True
        try:
            options={'accept_text':True} if accept_text else {}
            result=await self.api.draw(rid,row['score'],row['topic'],**options,
                before_post=lambda:self.store.authorize_post(rid),
                should_stop=lambda:bool(self.store.control(rid)['stop_requested']),
                on_job=lambda jid:self.store.set_job(rid,jid))
            if self.store.control(rid)['stop_requested']:return Reply('此抽题已请求停止，迟到结果未交付。用 /progress 确认。')
            if not self.store.change(rid,'drawing','active',result):return Reply('这次抽题已取消，未覆盖任何新题目。')
            row=dict(row,result=json.dumps(result,ensure_ascii=False))
            reply=self.problem(row,text=bool(result.get('_receipt_text_only')))
            reply.delivery=result.get('_receipt')
            return reply
        except (asyncio.TimeoutError, OSError) as exc:
            e=PracticeError('抽题连接中断，编号已保留；稍后 /roll 可查询原任务。');e.pending=True;raise e from None
        except PracticeError as exc:
            # A definitive rejection allows a new request; an uncertain transport error keeps the key for recovery.
            if getattr(exc,'definitive',False):self.store.change(rid,'drawing','error')
            raise
        finally:self.drawing.pop(rid,None)
