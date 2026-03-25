/**
 * Total Recall - OpenClaw Plugin
 * 
 * Auto flashback from memory-reflect before agent starts.
 * Injects relevant lessons and principles into context.
 */

import { beforePromptBuild } from './handler.js';

export default function register(api) {
  const cfg = api.config.plugins?.entries?.['total-recall']?.config ?? {};
  
  if (cfg.enabled === false) {
    api.logger.info('[total-recall] Plugin disabled');
    return;
  }

  api.logger.info('[total-recall] Initialized');

  // Register before_agent_start hook
  api.on('before_agent_start', async (event) => {
    try {
      const result = await beforePromptBuild(event);
      if (result?.prependContext) {
        api.logger.info(`[total-recall] Injecting ${result.prependContext.length} chars`);
      }
      return result;
    } catch (err) {
      api.logger.error(`[total-recall] Hook failed: ${err.message}`);
      return {};
    }
  });
}
