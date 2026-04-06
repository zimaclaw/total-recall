import {
  onMessageReceived,
  beforePromptBuild,
  onMessageSent,
} from './handler.js';

export default function register(api) {
  const cfg = api.config.plugins?.entries?.['total-recall']?.config ?? {};

  if (cfg.enabled === false) {
    api.logger.info('[total-recall] disabled');
    return;
  }

  api.logger.info('[total-recall] initialized');

  api.on('message_received', (event, ctx) => {
    try { onMessageReceived(event, ctx); } catch (err) {
      api.logger.error(`[total-recall] message_received: ${err.message}`);
    }
  });

  api.on('before_prompt_build', async (event, ctx) => {
    try {
      const result = await beforePromptBuild(event, ctx);
      if (result?.prependContext) {
        api.logger.info(`[total-recall] injected ${result.prependContext.length} chars`);
      }
      return result;
    } catch (err) {
      api.logger.error(`[total-recall] before_prompt_build: ${err.message}`);
      return {};
    }
  });

  api.on('agent_end', (event, ctx) => {
    try {
      api.logger.info(`[total-recall] agent_end: sessionId=${ctx?.sessionId} messages=${event?.messages?.length || 0}`);
      onMessageSent(event, ctx);
    } catch (err) {
      api.logger.error(`[total-recall] agent_end: ${err.message}`);
    }
  });
}
