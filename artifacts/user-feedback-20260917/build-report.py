"""Rebuild aggregate evidence and the report payload from the reviewed extract."""
import collections, datetime, json, pathlib, re, sqlite3
root = pathlib.Path(__file__).resolve().parent
x = json.loads((root/'evidence.json').read_text())
ss = x['sessions']
users = [m for s in ss for m in s['messages'] if m['role']=='user' and not m['text'].startswith('spot_')]
def category(m):
 t=m['display_text'] or m['text']
 if '重点看 level-0 区域' in t:return '明确矩形坐标'
 if '当前视野' in t:return '分析当前视野'
 if '找出最可疑' in t:return '寻找可疑区域'
 if '是否有肿瘤' in t:return '判断肿瘤组织'
 if '是什么细胞' in t:return '解释具体细胞'
 raise ValueError(t)
counts=collections.Counter(map(category,users))
rows=[{'intent':k,'count':v} for k,v in counts.most_common()]
fin=[e for s in ss for e in s['events'] if e['type']=='agent_finished']
metrics={'sessions':len(ss),'files':len({s['slide'] for s in ss}),'human_messages':len(users),'categories':dict(counts),'finished_sessions':sum(s['status']=='finished' for s in ss),'nonempty_final_events':sum(bool(e['payload'].get('summary','').strip()) for e in fin),'degraded_summary_calls':sum(m['tool_name']=='dev_sample_tma__slide_summary' and 'degraded' in m['text'] for s in ss for m in s['messages'])}
assert metrics['sessions']==7 and metrics['human_messages']==11 and metrics['nonempty_final_events']==8
assert sum(r['count'] for r in rows)==11
(root/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2))
md=(root.parent.parent/'docs/user-feedback-analysis-2026-09-17.md').read_text()
chunks=re.split(r'(?=^## )',md,flags=re.M)
blocks=[]
for i,chunk in enumerate(chunks):
 blocks.append({'id':f'section-{i}','type':'markdown','body':chunk.strip()})
 if chunk.startswith('## 1.'):
  blocks.append({'id':'intent-chart','type':'chart','chartId':'user-intents'})
  blocks.append({'id':'intent-interpretation','type':'markdown','sourceId':'sessions','body':'7 条“当前视野”加 1 条明确坐标，共 8 条范围指令，占本次 11 条用户指令的多数。图按指令条数计数，不按用户数计数；平台注入的标注上下文已排除。局部任务应有独立、清晰的入口。'})
sources=[{'id':'sessions','label':'U1 生产会话与终态事件（匿名化提取）','path':'evidence.json','query':{'description':'按经数据库验证的用户 ID 精确筛选 homepc sidecar-sessions，统计真实用户消息，排除 spot_updated；共 7 个会话。','executed_at':x['extracted_at'],'filters':['仅目标账号留存会话','北京时间 2026-09-16 15:22–17:52','排除自动标注上下文，不将其视为人类指令'],'metric_definitions':['每条真实用户指令按正文意图归入一个类别；总计 11 条。']}},{'id':'code','label':'HistoPilot 前端与 agent 源码；线上前端哈希一致','path':'source-notes.md'},{'id':'screenshots','label':'用户提供的四张微信反馈截图','path':'source-notes.md'}]
sql=(root/'intent-counts.sql').read_text()
conn=sqlite3.connect(':memory:')
conn.execute('CREATE TABLE session_extract (payload TEXT NOT NULL)')
conn.execute('INSERT INTO session_extract VALUES (?)',(json.dumps(x,ensure_ascii=False),))
rows_sql=[{'intent':r[0],'count':r[1]} for r in conn.execute(sql)]
assert {r['intent']:r['count'] for r in rows_sql}==dict(counts)
rows=rows_sql
sources[0]['query']={'sql':sql,'engine':'SQLite JSON1','tables_used':['session_extract'],'description':'将匿名化 evidence.json 加载到内存表 session_extract.payload，按真实用户消息分类计数。平台注入消息排除。','executed_at':x['extracted_at']}
sources[0]['path']='intent-counts.sql'
payload={'surface':'report','manifest':{'version':1,'surface':'report','title':'HistoPilot User Feedback Review','description':'用户 U1 的区域分析、回答质量与总结交付优化评审 · 2026 年 9 月 17 日','generatedAt':x['extracted_at'],'blocks':blocks,'sources':sources,'charts':[{'id':'user-intents','title':'本次用户指令类型','type':'bar','dataset':'intents','sourceId':'sessions','valueFormat':'number','encodings':{'x':{'field':'intent','type':'nominal','label':'指令类型'},'y':{'field':'count','type':'quantitative','label':'条数'}}}]},'snapshot':{'version':1,'status':'ready','generatedAt':x['extracted_at'],'datasets':{'intents':rows}},'sources':sources}
(root/'artifact.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
print(json.dumps(metrics,ensure_ascii=False,indent=2))
