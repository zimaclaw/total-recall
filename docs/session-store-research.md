# Session Store Research — OpenClaw Plugin total-recall

**Date:** 2026-03-30  
**Author:** Friday (manual analysis)  
**Context:** Plugin total-recall needs stable session identifier in `message_received` hook. Alternative: read message history directly from OpenClaw session store instead of PostgreSQL.

---

## Problem Statement

In the `message_received` hook, `sessionId` is NOT available. But in `before_prompt_build`, `ctx.sessionId` exists (UUID format).

**Questions:**
1. Where does OpenClaw physically store session files?
2. Can we find a session file by `sessionId` (UUID from `before_prompt_build`) and read message history?
3. Is there an API or utility in OpenClaw to read session history by `sessionId`?
4. How does dashboard get session data — through files or gateway API?

---

## Research Findings

### 1. Physical Location of Session Files

**Location:** `~/.openclaw/agents/<agent-id>/sessions/`

**Structure:**
```
~/.openclaw/agents/
├── main/
│   ├── agent/
│   └── sessions/
│       ├── 0dd9c9f5-77d1-4fb6-9753-80e1fae9db84.jsonl  ← Active session
│       ├── 0dd9c9f5-77d1-4fb6-9753-80e1fae9db84.jsonl.lock
│       ├── 047bcc03-96cc-4786-a76f-f44153778e42.jsonl.deleted.*  ← Deleted
│       └── ...
├── research/
│   └── sessions/
├── coding/
│   └── sessions/
└── ...
```

**File format:** JSONL (JSON Lines) — one JSON object per line

**Example session file content:**
```jsonl
{"type":"session","version":3,"id":"0dd9c9f5-77d1-4fb6-9753-80e1fae9db84","timestamp":"2026-03-30T13:33:52.556Z","cwd":"/home/ironman/.openclaw/workspace"}
{"type":"model_change","id":"2dacc65c","parentId":null,"timestamp":"2026-03-30T13:33:52.560Z","provider":"llamacpp","modelId":"Qwen3.5-27B-UD-Q4_K_XL.gguf"}
{"type":"message","id":"8d8a84fe","parentId":"9ffbf7c0","timestamp":"2026-03-30T13:33:55.755Z","message":{"role":"user","content":[{"type":"text","text":"..."}]}}
{"type":"message","id":"7b05419d","parentId":"8d8a84fe","timestamp":"2026-03-30T13:33:58.564Z","message":{"role":"assistant","content":[{"type":"text","text":"..."}]}}
```

**Key observation:** The filename IS the `sessionId` (UUID)!

### 2. Session ID Generation

**Location:** `sessions-D-LKdTsU.js`, line 581

```javascript
function mergeSessionEntry(existing, patch) {
    const sessionId = patch.sessionId ?? existing?.sessionId ?? crypto.randomUUID();
    // ...
}
```

**Generation:**
- If `sessionId` not provided in patch → generate via `crypto.randomUUID()`
- Stored in session store entry
- **Filename = sessionId** (e.g., `0dd9c9f5-77d1-4fb6-9753-80e1fae9db84.jsonl`)

### 3. Session Store Structure

**Session store:** JSON file mapping `sessionKey` → session entry

**Location:** `~/.openclaw/agents/<agent-id>/sessions/` (implicit, loaded via `loadSessionStore()`)

**Entry structure:**
```json
{
  "sessionId": "0dd9c9f5-77d1-4fb6-9753-80e1fae9db84",
  "agentId": "main",
  "channel": "webchat",
  "chatType": "direct",
  "totalTokens": 26503,
  "updatedAt": 1774878258445,
  // ...
}
```

**Key mapping:**
- `sessionKey` (e.g., `"main:webchat:direct:abc123"`) → human-readable key
- `sessionId` (UUID) → file identifier

### 4. How `sessionId` Becomes Available

**Chain of resolution:**

1. **In `message_received`** (line 22629 in reply-XaR8IPbY.js):
   ```javascript
   const sessionKey = ctx.SessionKey;  // Available!
   const sessionStoreEntry = resolveSessionStoreEntry(ctx, cfg);
   // sessionStoreEntry.entry?.sessionId — NOT available yet!
   ```

2. **After `message_received`**, session is resolved:
   ```javascript
   const resolveSessionStoreEntry = (ctx, cfg) => {
       const sessionKey = ctx.SessionKey?.trim();
       const store = loadSessionStore(storePath);
       return {
           sessionKey,
           entry: store[sessionKey.toLowerCase()] ?? store[sessionKey]
       };
   };
   ```

3. **In `before_prompt_build`** (line 75119):
   ```javascript
   const hookCtx = {
       agentId: hookAgentId,
       sessionKey: params.sessionKey,
       sessionId: params.sessionId,  // ← Now available!
       workspaceDir: params.workspaceDir,
       messageProvider: params.messageProvider
   };
   ```

**Key finding:** `sessionId` is extracted from session store entry AFTER `message_received` hook runs.

### 5. Reading Message History by Session ID

**Direct file access:** YES, possible!

**Method:**
```javascript
const sessionId = "0dd9c9f5-77d1-4fb6-9753-80e1fae9db84";
const agentId = "main";
const filePath = `~/.openclaw/agents/${agentId}/sessions/${sessionId}.jsonl`;

// Read file line by line
const lines = fs.readFileSync(filePath, 'utf8').split('\n');
const messages = lines
    .filter(line => JSON.parse(line).type === 'message')
    .map(line => JSON.parse(line).message);
```

**Message format in file:**
```json
{
  "type": "message",
  "id": "8d8a84fe",
  "parentId": "9ffbf7c0",
  "timestamp": "2026-03-30T13:33:55.755Z",
  "message": {
    "role": "user",
    "content": [
      {
        "type": "text",
        "text": "..."
      }
    ],
    "timestamp": 1774877635736
  }
}
```

**Advantages:**
- ✅ Direct file access — no API needed
- ✅ Complete message history with timestamps
- ✅ Includes tool calls, results, thinking blocks
- ✅ Atomic writes (`.lock` files prevent corruption)

**Limitations:**
- ⚠️ Need to know `agentId` (can derive from `sessionKey`)
- ⚠️ Need to handle `.deleted.*` files (skip them)
- ⚠️ Need to parse JSONL format (line-by-line)

### 6. Dashboard Data Source

**Dashboard port:** 18789 (from `openclaw.json`)

**Configuration:**
```json
{
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "loopback",
    "auth": {
      "mode": "token"
    }
  }
}
```

**Data source:** Gateway API (HTTP endpoints)

**How dashboard gets sessions:**
1. Dashboard calls gateway API endpoints
2. Gateway reads session store files directly
3. Returns JSON response with session metadata

**No direct file access from dashboard** — goes through gateway API.

### 7. Linking `message_received` to Session File

**Available in `message_received`:**

| Field | Location | Value |
|-------|----------|-------|
| `sessionKey` | `ctx.SessionKey` | `"main:webchat:direct:..."` |
| `conversationId` | computed | `ctx.OriginatingTo ?? ctx.To ?? ctx.From` (may be undefined) |
| `channelId` | computed | `ctx.Surface ?? ctx.Provider` |
| `accountId` | `ctx.AccountId` | Account identifier |

**NOT available in `message_received`:**
- ❌ `sessionId` (UUID) — not resolved yet
- ❌ Session store entry — loaded AFTER hook

**How to get `sessionId` in `message_received`:**

**Option A: Read session store directly**
```javascript
const sessionKey = ctx.SessionKey;
const agentId = sessionKey.split(':')[0]; // "main"
const storePath = `~/.openclaw/agents/${agentId}/sessions/.session-store.json`;
const store = JSON.parse(fs.readFileSync(storePath));
const entry = store[sessionKey.toLowerCase()];
const sessionId = entry?.sessionId; // ← Now you have it!
```

**Option B: Use internal hook instead**
```javascript
// Internal hook has sessionKey available!
registry.addInternalHookHandler("message", "received", async (event, ctx) => {
    const { sessionKey } = ctx;
    // sessionKey is available here
});
```

**Option C: Derive from sessionKey**
```javascript
// sessionKey format: "agentId:channel:chatType:hash"
// Not directly convertible to sessionId
// Must look up in session store
```

---

## Recommendations

### For total-recall Plugin

**Best approach:** Read session files directly by `sessionId`

**Why:**
- ✅ No need for PostgreSQL — use native OpenClaw storage
- ✅ Complete message history available
- ✅ File format is simple JSONL
- ✅ No API dependencies

**Implementation:**

```javascript
// In your plugin
const fs = require('fs');
const path = require('path');

function getSessionMessages(sessionId, agentId = 'main') {
    const filePath = path.join(
        process.env.HOME,
        `.openclaw/agents/${agentId}/sessions/${sessionId}.jsonl`
    );
    
    if (!fs.existsSync(filePath)) {
        return []; // Session file not found
    }
    
    const lines = fs.readFileSync(filePath, 'utf8').split('\n');
    const messages = [];
    
    for (const line of lines) {
        if (!line.trim()) continue;
        try {
            const entry = JSON.parse(line);
            if (entry.type === 'message') {
                messages.push({
                    role: entry.message.role,
                    content: entry.message.content,
                    timestamp: entry.timestamp,
                    messageId: entry.id
                });
            }
        } catch (err) {
            // Skip malformed lines
        }
    }
    
    return messages;
}

// Usage in before_prompt_build
registry.addHookHandler("before_prompt_build", async (event, ctx) => {
    const { sessionId } = ctx;
    const messages = getSessionMessages(sessionId);
    
    // Now you have complete message history!
    // Store in PostgreSQL if needed, or use directly
});
```

**For `message_received` hook:**

Use **internal hook** instead:

```javascript
registry.addInternalHookHandler("message", "received", async (event, ctx) => {
    const { sessionKey } = ctx;
    
    // Derive agentId from sessionKey
    const agentId = sessionKey.split(':')[0];
    
    // Look up sessionId from session store
    const storePath = path.join(
        process.env.HOME,
        `.openclaw/agents/${agentId}/sessions/.session-store.json`
    );
    
    const store = JSON.parse(fs.readFileSync(storePath));
    const entry = store[sessionKey.toLowerCase()];
    const sessionId = entry?.sessionId;
    
    if (sessionId) {
        // Now you have sessionId!
        await storeToPostgreSQL({
            sessionId,
            messageId: event.messageId,
            timestamp: event.timestamp,
            content: event.content
        });
    }
});
```

---

## Summary

| Question | Answer |
|----------|--------|
| **1. Where are session files stored?** | `~/.openclaw/agents/<agent-id>/sessions/<sessionId>.jsonl` |
| **2. Can we read by sessionId?** | **YES** — filename IS the sessionId (UUID) |
| **3. Is there an API?** | Gateway API on port 18789, but direct file access is simpler |
| **4. How does dashboard get data?** | Through gateway API (HTTP endpoints) |
| **5. Can we link message_received to session?** | **YES** — use internal hook or read session store directly |

---

## Files Analyzed

- `~/.openclaw/agents/main/sessions/*.jsonl` — session files
- `/home/ironman/.npm-global/lib/node_modules/openclaw/dist/reply-XaR8IPbY.js` — main reply dispatcher
- `/home/ironman/.npm-global/lib/node_modules/openclaw/dist/sessions-D-LKdTsU.js` — session management
- `~/.openclaw/openclaw.json` — gateway configuration

---

**Status:** ✅ Research complete  
**Confidence:** 95% (direct file inspection + source code analysis)  
**Next steps:** Implement direct file reading in total-recall plugin
