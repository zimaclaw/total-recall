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

  api.on('before_message_write', (event, ctx) => {
    try {
      api.logger.info(`[total-recall] before_message_write: role=${event?.message?.role} sessionId=${ctx?.sessionId}`);
      if (event?.message?.role === 'assistant') {
        onMessageSent(event, ctx);
      }
    } catch (err) {
      api.logger.error(`[total-recall] before_message_write: ${err.message}`);
    }
  });
}
