#!/bin/bash
# patch-mem0.sh — patches the hardcoded system prompt in mem0-oss.mjs
# Safe to re-run: checks if already patched before applying.

set -e

MEM0_JS="${1:-/home/ironman/.openclaw/extensions/openclaw-mem0/vendor/mem0-oss.mjs}"
BACKUP="$MEM0_JS.bak"

if [ ! -f "$MEM0_JS" ]; then
    echo "✗ File not found: $MEM0_JS"
    echo "  Usage: bash scripts/patch-mem0.sh [path/to/mem0-oss.mjs]"
    exit 1
fi

# Already patched?
if grep -q "memory extraction system for an AI agent" "$MEM0_JS"; then
    echo "✓ Already patched, skipping."
    exit 0
fi

# Backup
cp "$MEM0_JS" "$BACKUP"
echo "✓ Backup: $BACKUP"

# Build new prompt (single line, escaped for JS template literal)
NEW_PROMPT='You are a memory extraction system for an AI agent named Friday (Пятница, female). The owner of the system is Oleg (Олег, male).\n\nYour only task: extract facts, decisions, and conclusions worth remembering in future sessions.\n\nLANGUAGE RULES:\n- Preserve the original language of the conversation (Russian or English)\n- Keep technical terms, model names, commands, and service names as-is — do not translate\n- Never translate proper nouns: Пятница\/Friday, Олег\/Oleg, ollama, OpenClaw, docker, Qdrant, Neo4j, etc.\n- Write explanatory context in the language of the conversation\n\nEXTRACTION RULES:\n1. Extract only specific and significant information — no noise\n2. Do NOT extract: small talk, greetings, intermediate steps, obvious facts, time-bound statuses\n3. Each memory — one short sentence\n4. Detect the language of the user input and record facts in the same language\n\nEXTRACT:\n- Technical decisions and configurations (ports, models, parameters, file paths)\n- Errors and their causes\n- Decisions made and their rationale\n- Oleg'\''s preferences and working style\n- Active tasks and their status\n- Project and system component names\n- Lessons learned\n\nHere are few shot examples:\n\nInput: Hi.\nOutput: {"facts": []}\n\nInput: We changed the mem0 extraction model from mistral:7b to qwen2.5:7b-mem0 on port 11436.\nOutput: {"facts": ["mem0 extraction model: qwen2.5:7b-mem0, port 11436 (replaced mistral:7b)"]}\n\nInput: Олег предпочитает Python для бэкенда.\nOutput: {"facts": ["Олег предпочитает Python для бэкенда"]}\n\nReturn the facts in JSON format:\n{"facts": ["fact 1", "fact 2"]}\n\nYou MUST return a valid JSON object with a '\''facts'\'' key containing an array of strings.\n- Today'\''s date is ${(/* @__PURE__ */ new Date()).toISOString().split("T")[0]}.\n- DO NOT RETURN ANYTHING ELSE OTHER THAN THE JSON FORMAT.\n- DO NOT ADD ANY ADDITIONAL TEXT OR CODEBLOCK IN THE JSON FIELDS.\n- If nothing relevant found, return {"facts": []}.'

# Find line number of the prompt
LINE=$(grep -n "You are a Personal Information Organizer" "$MEM0_JS" | cut -d: -f1)

if [ -z "$LINE" ]; then
    echo "✗ Could not find original prompt anchor line. File may have changed."
    echo "  Restoring backup..."
    cp "$BACKUP" "$MEM0_JS"
    exit 1
fi

echo "  Found original prompt at line $LINE"

# Find the closing backtick of the template literal (next backtick after LINE)
END_LINE=$(awk "NR>$LINE && /^\`/{print NR; exit}" "$MEM0_JS")

if [ -z "$END_LINE" ]; then
    echo "✗ Could not find end of prompt template literal."
    cp "$BACKUP" "$MEM0_JS"
    exit 1
fi

echo "  Prompt spans lines $LINE–$END_LINE"

# Replace the content between the opening backtick on LINE and closing backtick on END_LINE
python3 - "$MEM0_JS" "$LINE" "$END_LINE" "$NEW_PROMPT" << 'PYEOF'
import sys

filepath = sys.argv[1]
start    = int(sys.argv[2])
end      = int(sys.argv[3])
new_prompt = sys.argv[4]

with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Replace lines start..end (1-indexed) with new template literal content
prefix = lines[start - 1][:lines[start - 1].index('`') + 1]  # keep `const systemPrompt = \``
suffix = '`'

new_lines = (
    lines[:start - 1] +
    [prefix + new_prompt + suffix + '\n'] +
    lines[end:]
)

with open(filepath, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f"  Replaced lines {start}–{end} with new prompt ({len(new_prompt)} chars)")
PYEOF

echo "✓ Patch applied: $MEM0_JS"
echo ""
echo "Restart OpenClaw gateway to apply changes:"
echo "  systemctl restart openclaw   # or your restart command"
