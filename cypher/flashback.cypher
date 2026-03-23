MATCH (t:Task)-[:HAS_CONCLUSION]->(c:Conclusion)
WHERE c.category  = $category
  AND c.confidence >= 0.6
RETURN c.insight, c.applies_when, c.confidence, c.evidence_type, t.outcome
ORDER BY c.confidence DESC LIMIT 5

UNION

MATCH (c:Conclusion)-[:GENERALIZES_TO]->(l:Lesson)
WHERE l.confidence  >= 0.75
  AND l.mastery     >= 0.6
  AND l.needs_review = false
RETURN l.principle, l.scope, l.confidence, 'lesson', ''
ORDER BY l.mastery DESC LIMIT 3;
