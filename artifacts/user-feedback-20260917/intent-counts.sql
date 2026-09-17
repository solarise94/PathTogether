WITH human_messages AS (
 SELECT COALESCE(NULLIF(json_extract(m.value, '$.display_text'), ''),
                 json_extract(m.value, '$.text')) AS message
 FROM session_extract AS e,
      json_each(e.payload, '$.sessions') AS s,
      json_each(s.value, '$.messages') AS m
 WHERE json_extract(m.value, '$.role') = 'user'
   AND substr(json_extract(m.value, '$.text'), 1, 5) <> 'spot_'
), classified AS (
 SELECT CASE
  WHEN instr(message, '重点看 level-0 区域') > 0 THEN '明确矩形坐标'
  WHEN instr(message, '当前视野') > 0 THEN '分析当前视野'
  WHEN instr(message, '找出最可疑') > 0 THEN '寻找可疑区域'
  WHEN instr(message, '是否有肿瘤') > 0 THEN '判断肿瘤组织'
  WHEN instr(message, '是什么细胞') > 0 THEN '解释具体细胞'
  ELSE '未分类'
 END AS intent FROM human_messages
)
SELECT intent, COUNT(*) AS count
FROM classified
GROUP BY intent
ORDER BY count DESC, intent ASC;
