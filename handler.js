import { execSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';

const LOG = '/tmp/total-recall.log';
const MEMORY_DIR = '/home/ironman/.openclaw/skills/memory-reflect';

function log(msg) {
  writeFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`, { flag: 'a' });
}

function inferCategory(prompt) {
  const text = (prompt || '').toLowerCase();
  
  // Приоритетные категории — проверяем первыми
  // Если есть явное упоминание deployment/deploy service — это deploy, не infra
  const deployPatterns = [
    /deploy\s+(gateway|service|app|version|new)/,  // "deploy gateway", "deploy new version"
    /задеплой/,  // русское "задеплой"
    /deployment/,
    /release\s+(new|latest|v\d)/,  // "release new", "release v1.0"
  ];
  
  for (const pattern of deployPatterns) {
    if (pattern.test(text)) {
      return 'deploy';
    }
  }
  
  const keywords = {
    infra: ['server', 'docker', 'nginx', 'port', 'network', 'сервер', 'порт', 'systemd', 'nginx', 'reverse', 'proxy'],
    dev: ['code', 'script', 'bug', 'fix', 'function', 'git', 'код', 'скрипт', 'баг', 'python', 'node'],
    memory: ['memory', 'remember', 'flashback', 'reflect', 'память', 'вспомни', 'принцип'],
    research: ['research', 'find', 'search', 'analyze', 'исследуй', 'найди', 'поищи'],
    test: ['test', 'check', 'validate', 'verify', 'тест', 'проверь'],
    deploy: ['deploy', 'release', 'publish', 'деплой', 'релиз', 'deployments', 'ci', 'cd', 'pipeline'],
    plan: ['plan', 'schedule', 'roadmap', 'план', 'роадмап'],
    write: ['write', 'document', 'report', 'напиши', 'документ'],
  };
  
  const scores = {};
  for (const [cat, kws] of Object.entries(keywords)) {
    scores[cat] = kws.filter(kw => text.includes(kw)).length;
  }
  
  const best = Object.entries(scores).sort((a, b) => b[1] - a[1])[0];
  return best[1] > 0 ? best[0] : 'dev';
}

function runFlashback(category) {
  const cmd = `cd ${MEMORY_DIR} && poetry run python memory-reflect.py --flashback --category ${category}`;
  try {
    return execSync(cmd, { encoding: 'utf8', timeout: 10000 });
  } catch (err) {
    log(`flashback error: ${err.message}`);
    return '';
  }
}

function formatContext(raw, category) {
  if (!raw?.trim()) return '';
  
  const lines = raw.split('\n')
    .filter(l => !/\d{4}-\d{2}-\d{2}.*\[(INFO|WARNING|ERROR)\]/.test(l) && l.trim())
    .join('\n').trim();
  
  if (!lines) return '';
  
  return `=== MEMORY CONTEXT [${category}] ===\n${lines}\n=== END MEMORY CONTEXT ===`;
}

export async function beforePromptBuild(event) {
  const prompt = event?.prompt || '';
  if (!prompt) return {};
  
  const category = inferCategory(prompt);
  log(`category=${category} prompt="${prompt.substring(0, 60)}"`);
  
  const raw = runFlashback(category);
  const prependContext = formatContext(raw, category);
  
  if (prependContext) {
    log(`injected ${prependContext.length} chars`);
  }
  
  return { prependContext };
}
